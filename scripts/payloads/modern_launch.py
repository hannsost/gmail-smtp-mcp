"""Preset payload for modern launch announcement."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def build_payload() -> dict:
    now = datetime.now().astimezone()
    project_root = Path(__file__).resolve().parents[2]
    return {
        "to": ["kjmsumatra@gmail.com"],
        "subject": "Nimbus UI launch preview",
        "body": "(fallback text)",
        "body_template": "modern_launch",
        "template_variables": {
            "headline": "Nimbus UI beta is live",
            "subheadline": "A faster way to ship internal tools with confidence.",
            "recipient_name": "Hanns",
            "intro": "We’ve been quietly expanding the beta and would love your feedback.",
            "highlights": (
                "<li>Visual builder with production-grade data binding</li>"
                "<li>Audit-friendly version history</li>"
                "<li>Deep GitHub + Slack integration</li>"
            ),
            "body_paragraph": "Let’s walk through the roadmap and align on launch blockers.",
            "cta_label": "Review the launch plan",
            "cta_url": "https://example.com/nimbus-plan",
            "cta_caption": "Secure workspace link.",
            "closing": "Talk soon!",
            "footer_note": "Nimbus UI · Remote-first team · hello@nimbus.dev",
        },
        "signature_template": "work",
        "signature_variables": {
            "name": "Codex Automation",
            "role": "Launch Coordinator",
            "company": "Nimbus UI",
            "email": "codex@nimbus.dev",
            "phone": "+49 30 000000",
            "tagline": "Building reliable automation since 2012.",
        },
        "inline_images": [
            {
                "path": "/tmp/codex-inline.png",
                "cid": "codex-inline",
                "ensure_sample_png": True,
            }
        ],
        "attachments": [str((project_root / "README.md").resolve())],
        "calendar_event": {
            "summary": "Nimbus launch sync",
            "start": (now + timedelta(minutes=45)).isoformat(),
            "end": (now + timedelta(minutes=75)).isoformat(),
            "description": "Walkthrough of the Nimbus UI launch plan.",
            "location": "Google Meet",
        },
        "diagnostics": True,
    }
