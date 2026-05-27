services:
  - type: web
    name: telegram-image-bot
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python bot.py
    envVars:
      - key: BOT_TOKEN
        sync: false
      - key: IMGBB_API_KEY
        sync: false
