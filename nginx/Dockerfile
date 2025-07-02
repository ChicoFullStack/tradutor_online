# Usa a imagem oficial e leve do Nginx baseada em Alpine Linux.
FROM nginx:1.25-alpine

# Remove a configuração padrão do Nginx.
RUN rm /etc/nginx/conf.d/default.conf

# Copia o seu ficheiro de configuração personalizado para a pasta de configuração do Nginx.
COPY nginx.conf /etc/nginx/nginx.conf

# Expõe as portas 80 (HTTP) e 443 (HTTPS) para o exterior.
EXPOSE 80
EXPOSE 443

# O comando para iniciar o Nginx em modo "foreground".
CMD ["nginx", "-g", "daemon off;"]
