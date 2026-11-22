FROM python:3.13.9-slim-bookworm
WORKDIR /bot
COPY arena.py bot.py dodge.py mechfight.py ./
ENTRYPOINT ["python", "-u", "/bot/bot.py"]
