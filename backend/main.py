import asyncio
import json
import logging
import base64
from typing import Dict, Set, List, Tuple

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Importações das APIs do Google Cloud
from google.cloud import speech, translate_v2 as translate, texttospeech

# --- Configuração Inicial ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Estruturas de Dados para a Arquitetura SFU ---

class Participant:
    """Representa um único participante na chamada."""
    def __init__(self, user_id: str, websocket: WebSocket):
        self.user_id = user_id
        self.websocket = websocket
        self.pc = RTCPeerConnection()
        self.source_language = "pt-BR"  # Idioma que o utilizador fala
        self.target_language = "en-US"  # Idioma para o qual ele quer receber traduções
        self.translation_mode = "text_only"  # 'text_only' ou 'audio_and_text'
        self.audio_track = None
        self.video_track = None

class Room:
    """Gere todos os participantes e faixas de mídia numa sala."""
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.participants: Dict[str, Participant] = {}

    async def add_participant(self, user_id: str, websocket: WebSocket) -> Participant:
        participant = Participant(user_id, websocket)
        
        @participant.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                await participant.websocket.send_json(
                    {"type": "candidate", "candidate": candidate.to_dict()}
                )

        @participant.pc.on("track")
        async def on_track(track):
            logger.info(f"Faixa '{track.kind}' recebida de {user_id}")
            if track.kind == "audio":
                participant.audio_track = track
                # Passa o idioma de origem do orador para o processador
                processor = AudioProcessor(self, participant, participant.source_language)
                asyncio.create_task(processor.start())
            elif track.kind == "video":
                participant.video_track = track
            await self.add_track_to_others(participant)

        self.participants[user_id] = participant
        return participant

    async def remove_participant(self, user_id: str):
        if user_id in self.participants:
            participant_to_remove = self.participants[user_id]
            await participant_to_remove.pc.close()
            del self.participants[user_id]
            for other_participant in self.participants.values():
                for sender in other_participant.pc.getSenders():
                    if sender.track and hasattr(sender.track, 'id') and sender.track.id.startswith(user_id):
                        try:
                            other_participant.pc.removeTrack(sender)
                        except Exception as e:
                            logger.warning(f"Erro ao remover faixa: {e}")
            logger.info(f"Participante {user_id} removido da sala {self.room_id}")

    async def add_track_to_others(self, new_participant: Participant):
        for other_participant in self.participants.values():
            if other_participant.user_id != new_participant.user_id:
                if new_participant.audio_track: other_participant.pc.addTrack(new_participant.audio_track)
                if new_participant.video_track: other_participant.pc.addTrack(new_participant.video_track)
                if other_participant.audio_track: new_participant.pc.addTrack(other_participant.audio_track)
                if other_participant.video_track: new_participant.pc.addTrack(other_participant.video_track)

rooms: Dict[str, Room] = {}

# --- Processamento de Áudio e Tradução ---

class AudioProcessor:
    """Processa o áudio de um participante para transcrição, tradução e, opcionalmente, síntese de voz."""
    def __init__(self, room: Room, speaker: Participant, source_language: str):
        self.room = room
        self.speaker = speaker
        self.source_language = source_language
        self.speech_client = speech.SpeechClient()
        self.translate_client = translate.Client()
        self.tts_client = texttospeech.TextToSpeechClient()

    async def start(self):
        # Usa o idioma de origem do orador para a transcrição
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
            sample_rate_hertz=48000,
            language_code=self.source_language,
            enable_automatic_punctuation=True,
        )
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=False)
        
        try:
            async for response in self.speech_client.streaming_recognize(
                requests=(speech.StreamingRecognizeRequest(audio_content=chunk) async for chunk in self.audio_stream_generator()),
                config=streaming_config,
            ):
                await self.process_responses(response)
        except Exception as e:
            logger.error(f"Erro no processamento de áudio para {self.speaker.user_id}: {e}")

    async def audio_stream_generator(self):
        if not self.speaker.audio_track: return
        while True:
            try:
                yield (await self.speaker.audio_track.recv()).to_bytes()
            except (asyncio.CancelledError, Exception) as e:
                logger.warning(f"Erro ao receber frame de áudio: {e}"); break

    async def process_responses(self, response):
        for result in response.results:
            if result.is_final and result.alternatives:
                transcript = result.alternatives[0].transcript
                logger.info(f"Transcrição de {self.speaker.user_id} ({self.source_language}): {transcript}")
                
                for participant in self.room.participants.values():
                    if participant.user_id != self.speaker.user_id:
                        target_lang = participant.target_language
                        translated_text = self.translate_text(transcript, target_lang)
                        
                        if translated_text:
                            await participant.websocket.send_json({"type": "subtitle", "text": translated_text, "from_user_id": self.speaker.user_id})
                            if participant.translation_mode == "audio_and_text":
                                audio_content = self.synthesize_speech(translated_text, target_lang)
                                if audio_content:
                                    encoded_audio = base64.b64encode(audio_content).decode('utf-8')
                                    await participant.websocket.send_json({"type": "translated_audio", "audio_content": encoded_audio, "from_user_id": self.speaker.user_id})

    def translate_text(self, text: str, target_language: str) -> str:
        try:
            return self.translate_client.translate(text, target_language=target_language)["translatedText"]
        except Exception as e:
            logger.error(f"Erro na tradução: {e}"); return ""

    def synthesize_speech(self, text: str, language_code: str) -> bytes:
        try:
            synthesis_input = texttospeech.SynthesisInput(text=text)
            voice = texttospeech.VoiceSelectionParams(language_code=language_code, ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL)
            audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
            response = self.tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
            return response.audio_content
        except Exception as e:
            logger.error(f"Erro na síntese de voz: {e}"); return b""

# --- Endpoint WebSocket ---
@app.websocket("/ws/{room_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, user_id: str):
    await websocket.accept()
    if room_id not in rooms: rooms[room_id] = Room(room_id)
    room = rooms[room_id]
    participant = await room.add_participant(user_id, websocket)
    logger.info(f"Utilizador {user_id} entrou na sala {room_id}. Total: {len(room.participants)}")
    
    for p in room.participants.values():
        if p.user_id != user_id: await p.websocket.send_json({"type": "user-joined", "user_id": user_id})

    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")

            if message_type == "offer":
                offer = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
                participant.source_language = data.get("source_lang", "pt-BR")
                participant.target_language = data.get("target_lang", "en-US")
                await participant.pc.setRemoteDescription(offer)
                answer = await participant.pc.createAnswer()
                await participant.pc.setLocalDescription(answer)
                await websocket.send_json({"type": "answer", "answer": {"sdp": participant.pc.localDescription.sdp, "type": participant.pc.localDescription.type}})
            
            elif message_type == "language-settings-change":
                participant.source_language = data.get("source_lang", participant.source_language)
                participant.target_language = data.get("target_lang", participant.target_language)
                logger.info(f"Utilizador {user_id} alterou idiomas: Fala '{participant.source_language}', Traduz para '{participant.target_language}'")

            elif message_type == "translation-mode-change":
                mode = data.get("mode", "text_only")
                if mode in ["text_only", "audio_and_text"]:
                    participant.translation_mode = mode
                    logger.info(f"Utilizador {user_id} alterou modo de tradução para {mode}")
            
            elif message_type == "speaking":
                for p in room.participants.values():
                    if p.user_id != user_id: await p.websocket.send_json({"type": "speaking", "speaking": data.get("speaking"), "from_user_id": user_id})
    
    except WebSocketDisconnect:
        logger.info(f"Utilizador {user_id} desconectou-se.")
    finally:
        await room.remove_participant(user_id)
        for p in room.participants.values(): await p.websocket.send_json({"type": "user-left", "user_id": user_id})
        if not room.participants: del rooms[room_id]; logger.info(f"Sala {room_id} vazia, a remover.")
