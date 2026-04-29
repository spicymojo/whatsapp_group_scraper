# 📰 WhatsApp Group Scraper

A robust, automated WhatsApp bot that monitors a specific group for a daily newspaper PDF, downloads it, renames it with a clean Spanish date format, and forwards it to a designated contact. Built with Python and the [neonize](https://github.com/krypton-byte/neonize) library.

## ✨ Features

* **Targeted Monitoring:** Scans specific WhatsApp groups for files matching a keyword (e.g., "La Monda").
* **Smart Renaming:** Automatically converts raw filenames into a clean format (e.g., `La Monda, 16 de Marzo.pdf`).
* **Resilient Downloading:** Uses a 3-tier fallback strategy (Raw Message -> Pointer -> Low-Level Decryption) to bypass WhatsApp Web mesh `wire-format` sync errors.
* **Daily Lockdown:** Creates a persistent `last_sent.txt` log to ensure the file is only forwarded once per day, even if the script or the server restarts.
* **Quiet Hours:** Ignores files sent before 7:00 AM to avoid premature triggers.
* **Secure Configuration:** Uses `.env` variables so your personal phone numbers and group IDs are never exposed in the code.

## 📋 Prerequisites

* Python 3.8 or higher.
* A linked WhatsApp device (the script generates a `session.db` file upon first login).
* **For Raspberry Pi:** A terminal multiplexer like `tmux` or `systemd` to keep the script running in the background.

## 🚀 Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/yourusername/whatsapp_group_scraper.git](https://github.com/yourusername/whatsapp_group_scraper.git)
   cd whatsapp_group_scraper
   ```

2. **Create a virtual environment (Recommended):**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install neonize python-dotenv
   ```

4. **Configure your environment variables:**
   * Copy the example environment file:
     ```bash
     cp .env.example .env
     ```
   * Open `.env` and fill in your details:
     ```ini
     TARGET_GROUP_ID=1234567890@g.us       # The ID of the group to monitor
     TARGET_RECIPIENT=34600000000          # The phone number to forward the PDF to
     SEARCH_TERM=La Monda                  # The keyword to look for in the filename
     ```

## 💻 Usage (Local Testing)

Run the script locally for the first time:
```bash
python scraper.py
```
* **First Login:** The console will output a QR code. Scan it with your WhatsApp app (Linked Devices) to authenticate.
* Once authenticated, a `session.db` file will be created in your folder. **Do not commit this file to GitHub.**

## 🍓 Raspberry Pi Deployment

To run this 24/7 on a Raspberry Pi without needing to scan the QR code again:

1. **Transfer the Session:** Copy your locally generated `session.db` file directly to the project folder on your Raspberry Pi via SCP or SFTP.
2. **Pull the code & install requirements:** Run the installation steps (1-3) on your Pi.
3. **Keep it running:** You can use `tmux` or create a `systemd` service.

### Option: Running as a `systemd` Background Service (Recommended)

Running it as a service ensures the bot automatically restarts if it crashes or if the Raspberry Pi reboots.

1. Create a service file:
   ```bash
   sudo nano /etc/systemd/system/wabot.service
   ```
2. Paste the following configuration (adjust the paths to match your Pi):
   ```ini
   [Unit]
   Description=WhatsApp Newspaper Bot
   After=network.target

   [Service]
   Type=simple
   User=pi
   WorkingDirectory=/home/pi/whatsapp_group_scraper
   ExecStart=/home/pi/whatsapp_group_scraper/.venv/bin/python scraper.py
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start the service:
   ```bash
   sudo systemctl enable wabot.service
   sudo systemctl start wabot.service
   ```
4. Check the logs:
   ```bash
   sudo journalctl -u wabot.service -f
   ```

## 📂 File Structure

* `scraper.py`: The main bot logic, event listeners, and download/upload strategies.
* `naming_utils.py`: Helper functions to translate dates and format strings into Spanish.
* `.env`: Your private configuration file (ignored by git).
* `last_sent.txt`: Automatically generated file tracking the date of the last successful forward.
* `downloads/`: Automatically generated folder where PDFs are temporarily stored.

## ⚠️ Disclaimer

This project is for educational purposes and personal automation. Please ensure you comply with WhatsApp's Terms of Service regarding automated messaging.