# Bot profile photo

`bot-avatar.png` (640x640) is the bot's profile picture: a wallet with a coin, rising bars and a trend arrow, in the app's blue. Telegram shows it as a circle, and everything important sits inside that circle.

To set it:

1. Open @BotFather in Telegram and send `/setuserpic`.
2. Pick your bot.
3. Send `bot-avatar.png` as a photo.

`make_avatar.py` regenerates the image (needs Pillow and NumPy).
