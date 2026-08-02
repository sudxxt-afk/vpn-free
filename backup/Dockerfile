FROM postgres:16-alpine
RUN apk add --no-cache openssl
COPY backup.sh /usr/local/bin/backup.sh
RUN sed -i 's/\r$//' /usr/local/bin/backup.sh && chmod +x /usr/local/bin/backup.sh
ENTRYPOINT ["/usr/local/bin/backup.sh"]
