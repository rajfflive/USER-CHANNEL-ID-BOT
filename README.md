# ID Checker Bot

Telegram bot to fetch IDs of users, groups, channels, forums etc. with force‑join feature and admin panel.

## Features
- Get ID of any user, premium user, bot, group, channel, forum
- Forward any message to get sender ID
- Add bot to group/channel to get its ID
- Force‑join channels/groups
- Admin panel with stats, broadcast, force‑join management

## Join Our Channel for Updates
🔔 **Stay updated with new features and bot status:**  
[Join Telegram Channel](https://t.me/+_IL16SZ7apBiZWI1)

## Deploy on Render

1. Fork/clone this repo.
2. Create a new **Worker** service on Render.
3. Connect your GitHub repo.
4. Set environment variables:
   - `BOT_TOKEN`
   - `ADMIN_IDS` (comma‑separated)
   - `LOG_CHANNEL_ID` (optional)
5. Build Command: `pip install -r requirements.txt`
6. Start Command: `python main.py`
7. Deploy!

## Storage
Bot uses a local `store.json` file to store user/group/channel IDs and force‑join channels.  
⚠️ This file is ephemeral on Render – it will be lost on redeploy. For production, consider using a database (MongoDB, Redis, etc.). You can modify the `load_store`/`save_store` functions accordingly.

---

### Commands
- `/start` – Show your own ID
- `/id` – Get current chat ID
- `/help` – How to use
- `/admin` – Admin panel (only for admins)
- `/addforcejoin @channel` – Add force‑join channel/group
- `/removeforcejoin @channel` – Remove force‑join channel/group
- `/broadcast` (reply to a message) – Broadcast to all users/groups/channels

---

### License
MIT
