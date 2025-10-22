# Gmail SMTP MCP Server

This directory contains a minimal [FastMCP](https://github.com/modelcontextprotocol) server that sends Gmail messages (including file attachments) using SMTP credentials. A local virtual environment in `.venv/` provides the `mcp` Python dependency (see `requirements.txt`).

## Files

- `server.py` — exposes the `gmail_send_email_with_attachments` tool.
- `requirements.txt` — pinned dependencies for the MCP server runtime.
- `gmail_smtp.example.env` — template for configuring Gmail SMTP credentials.
- `~/.config/mcp/gmail_smtp.env` — dotenv-style file loaded for credentials (username, app password, etc.). Set `GMAIL_SMTP_ENV_FILE` to point elsewhere if desired.

## Usage

1. Install dependencies:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
2. Ensure the environment file has valid Gmail SMTP values.
3. Copy `gmail_smtp.example.env` to `~/.config/mcp/gmail_smtp.env` (or set `GMAIL_SMTP_ENV_FILE`) and fill in credentials.
4. Start Codex CLI; it will load this MCP server via the entry in `~/.codex/config.toml` (pointing at `.venv/bin/python`).
5. Call the tool with:
   ```json
   {
     "to": ["recipient@example.com"],
     "subject": "Your Subject",
     "body": "Plain-text body",
     "attachments": ["/absolute/path/to/file"]
   }
   ```
   Optional fields: `cc`, `bcc` (lists of strings).

The tool builds MIME messages, guesses attachment media types, and issues the SMTP send asynchronously.

## Security

The Gmail app password in `gmail_smtp.env` is sensitive. Rotate or remove it if you no longer need this configuration.
