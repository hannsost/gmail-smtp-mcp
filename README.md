# Gmail SMTP MCP Server

This directory contains an [FastMCP](https://github.com/modelcontextprotocol) server that uses a Gmail app password to send rich emails and inspect inbox metadata. A local virtual environment in `.venv/` provides the `mcp` Python dependency (see `requirements.txt`).

## Capabilities

- **Rich sending** – plain-text + HTML bodies, reusable templates, signatures, inline images (CID embeds), CC/BCC, attachments.
- **Calendar invites** – automatically generate `.ics` calendar requests without using Google Calendar APIs.
- **Diagnostics** – optional SMTP telemetry (TLS handshake, EHLO features, NOOP response).
- **Inbox snapshots** – list unread mail, search by subject, or fetch the latest message from a sender via IMAP.
- **Email spooler** – queue payloads locally and deliver them later from an environment with SMTP connectivity.

## Files

- `server.py` — MCP tools (SMTP send + IMAP helpers) and template rendering logic.
- `requirements.txt` — pinned dependencies for the MCP server runtime.
- `gmail_smtp.example.env` — template for configuring Gmail SMTP/IMAP credentials.
- `templates/` — sample text/HTML templates (`meeting_followup`, `status_digest`, `modern_launch`). Add your own as needed.
- `signatures/` — reusable signatures (`work`, `personal`) in text/HTML form.
- `spool/` — queue directories (`pending/`, `sent/`, `failed/`) for offline delivery workflows.
- `scripts/` — helpers for sending presets, queueing, and delivering spool payloads.
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
  "signature_template": "work",
  "signature_variables": {
    "name": "Alex Johnson",
    "role": "Eng Manager",
    "company": "Acme Robotics",
    "email": "alex@example.com",
    "phone": "+1-555-0100",
    "tagline": "Building reliable automation since 2012."
  },
  "diagnostics": true
}
```

Notes:

- Provide either `body` (plain text) or `body_template`. When using templates, matching `.txt` and `.html` files are loaded from `templates/` and rendered with `template_variables`.
- Inline images require an HTML body/template; reference them in HTML with `cid:company-logo`.
- Signatures behave like templates and render from `signatures/<name>.txt` / `.html`. When `signature_variables` is omitted the `template_variables` map is reused.
- Calendar invites attach an `.ics` file (METHOD:REQUEST) that works with most clients.

### Modern launch template example

```json
{
  "to": ["recipient@example.com"],
  "subject": "We’re live!",
  "body": "(fallback text)",
  "body_template": "modern_launch",
  "template_variables": {
    "headline": "Introducing Nimbus UI",
    "subheadline": "A faster way to prototype internal tools.",
    "recipient_name": "Jamie",
    "intro": "After months of refining the experience, we’re ready to share Nimbus UI with you.",
    "highlights": "<li>Drag-and-drop layout builder</li><li>Production-ready data bindings</li><li>Audit-friendly change history</li>",
    "body_paragraph": "We would love to give you an in-depth walkthrough and hear what problems you are solving this quarter.",
    "cta_label": "Book a walkthrough",
    "cta_url": "https://example.com/demo",
    "cta_caption": "Pick any slot that works for you.",
    "closing": "Looking forward to chatting!",
    "footer_note": "Nimbus UI · 123 Market Street · Berlin"
  },
  "signature_template": "personal",
  "signature_variables": {
    "name": "Hanns Ost",
    "favorite_quote": "\"Stay curious, ship often.\"",
    "website_label": "Website",
    "website": "https://hannsost.dev",
    "social_label": "Mastodon",
    "social_link": "https://mastodon.social/@hannsost"
  }
}
```

### Using the preset sender script
### Preparing sample assets via tool

Call the MCP tool `gmail_prepare_sample_assets` when you need ready-to-use files. It creates small attachments under `attachments/` and an inline PNG in `assets/` and returns the absolute paths.
Use the returned values when invoking `gmail_send_email_with_attachments` directly (relieves you from having to create files manually inside the sandbox).

### Managing templates and signatures

Use the following MCP tools before sending live emails:

- `gmail_list_templates` / `gmail_list_signatures` — list existing snippets and preview the first few characters of the text/HTML files.
- `gmail_create_template` / `gmail_create_signature` — add new entries. If you omit bodies, defaults are generated automatically. Set `overwrite=true` to replace existing files.
- `gmail_list_sample_assets` — return the current sample attachments and inline image paths without regenerating them.

This lets you script a full workflow: list, create, stage assets, then send or queue the email payload.


For reproducible tests you can run predefined payloads stored in `scripts/payloads/`:

```bash
# Optional: re-create the inline sample image
python3 - <<'PY'
from pathlib import Path
png = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4890000000A49"
    "444154789C6360000002000100ABFE28D90000000049454E44AE426082"
)
Path("/tmp/codex-inline.png").write_bytes(png)
PY

# Send the modern launch preset
.venv/bin/python scripts/send_email.py modern_launch
```

Add more payloads by creating modules inside `scripts/payloads/` that expose a `build_payload()` function.

### Queueing and delivering via spool

When the execution environment cannot reach Gmail directly, queue the payload and deliver it later from a machine with SMTP access:

```bash
# Queue a message without sending it
.venv/bin/python scripts/send_email.py modern_launch --queue-only

# Inspect queued files (JSON) under spool/pending/
ls spool/pending

# Deliver everything in the queue (run this where Gmail is reachable)
.venv/bin/python scripts/deliver_spool.py

# Optional dry run
.venv/bin/python scripts/deliver_spool.py --dry-run
```

Successful deliveries move the entry to `spool/sent/`; failures (with traceback) land in `spool/failed/`.
You can also queue programmatically via the MCP tool `gmail_queue_email_with_attachments`; provide the same JSON payload fields and the spooler will deliver on its next pass.

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
