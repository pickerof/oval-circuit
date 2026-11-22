FROM python:3.13.9-slim-bookworm
WORKDIR /bot
COPY arena.py bot.py dodge.py mechfight.py hybrid.py reclaimer_bot.py reclaimer_arena.py reclaimer_dodge.py ./
ENTRYPOINT ["python", "-u", "/bot/hybrid.py"]
