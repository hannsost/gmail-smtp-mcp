# Gmail SMTP MCP Server

This directory contains an [FastMCP](https://github.com/modelcontextprotocol) server that uses a Gmail app password to send rich emails and inspect inbox metadata. A local virtual environment in `.venv/` provides the `mcp` Python dependency (see `requirements.txt`).

## Capabilities

- **Rich sending** – plain-text + HTML bodies, reusable templates, inline images (CID embeds), CC/BCC, attachments.
- **Calendar invites** – automatically generate `.ics` calendar requests without using Google Calendar APIs.
- **Diagnostics** – optional SMTP telemetry (TLS handshake, EHLO features, NOOP response).
- **Inbox snapshots** – list unread mail, search by subject, or fetch the latest message from a sender via IMAP.

## Files

- `server.py` — MCP tools (SMTP send + IMAP helpers) and template rendering logic.
- `requirements.txt` — pinned dependencies for the MCP server runtime.
- `gmail_smtp.example.env` — template for configuring Gmail SMTP/IMAP credentials.
- `templates/` — sample text/HTML templates (`meeting_followup`, `status_digest`). Add your own as needed.
- `~/.config/mcp/gmail_smtp.env` — dotenv-style file loaded for credentials (username, app password, etc.). Set `GMAIL_SMTP_ENV_FILE` to point elsewhere if desired.

## Configuration

Copy the example env file (or point `GMAIL_SMTP_ENV_FILE` to another path) and fill in your Gmail details:

```bash
cp gmail_smtp.example.env ~/.config/mcp/gmail_smtp.env
```

Required keys:

- `GMAIL_SMTP_USERNAME` – full Gmail address
- `GMAIL_SMTP_APP_PASSWORD` – 16-character app password generated at https://myaccount.google.com/apppasswords

Optional keys:

- `GMAIL_FROM_ADDRESS` – override the From header (defaults to the username)
- `GMAIL_SMTP_SERVER` / `GMAIL_SMTP_PORT` – SMTP host/port (defaults to Gmail SSL 465)
- `GMAIL_IMAP_SERVER` / `GMAIL_IMAP_PORT` – IMAP host/port (defaults to Gmail SSL 993)

## Usage

1. Install dependencies:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
2. Provide valid Gmail credentials via the env file or exported environment variables.
3. Register this MCP server in Codex CLI (e.g. point to `.venv/bin/python server.py`).
4. Call the tools, for example:

### Sending rich email

```json
{
  "to": ["recipient@example.com"],
  "cc": ["cc@example.com"],
  "subject": "Project kickoff",
  "body_template": "meeting_followup",
  "template_variables": {
    "name": "Jamie",
    "notes": "<li>Agreed on scope</li><li>Shared roadmap deck</li>",
    "next_steps": "<li>Send contract</li><li>Book sprint planning</li>",
    "sender_name": "Alex"
  },
  "inline_images": [{"path": "~/Pictures/logo.png", "cid": "company-logo"}],
  "attachments": ["/absolute/path/to/deck.pdf"],
  "calendar_event": {
    "summary": "Sprint planning",
    "start": "2025-10-25T09:00:00+02:00",
    "end": "2025-10-25T10:00:00+02:00",
    "location": "Online",
    "description": "Kick off the first sprint.",
    "attendees": [{"email": "recipient@example.com", "name": "Jamie"}]
  },
  "diagnostics": true
}
```

Notes:

- Provide either `body` (plain text) or `body_template`. When using templates, matching `.txt` and `.html` files are loaded from `templates/` and rendered with `template_variables`.
- Inline images require an HTML body/template; reference them in HTML with `cid:company-logo`.
- Calendar invites attach an `.ics` file (METHOD:REQUEST) that works with most clients.

### Inbox helpers

The following read-only tools leverage IMAP (same app password) and return lightweight previews:

- `gmail_list_unread_messages(limit=10)` – latest unread messages.
- `gmail_search_subject(subject, limit=10)` – search by subject string (UTF-8).
- `gmail_fetch_latest_from_sender(sender)` – newest message from an address.

Each preview contains the message UID, subject, sender, ISO-8601 date when available, and a small body snippet.

## Testing

Use Codex CLI (or `python server.py` directly) to invoke tools. For end-to-end tests you can send mails to `kjmsumatra@gmail.com` or `hannsost@gmail.com`.

Because Gmail app passwords bypass 2FA but still authenticate SMTP/IMAP, rotate the password if you suspect compromise.

## Security

- Treat `gmail_smtp.env` as sensitive material; do not commit it.
- Regenerate the Gmail app password if you replace or publish this MCP server.
- Calendar invites and inline images are generated locally—review attachments before sending if templates consume user input.
