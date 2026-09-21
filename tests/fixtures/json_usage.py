#!/usr/bin/env python3
import json
print(json.dumps({
    "accounts": [
        {
            "label": "JSON Account",
            "status": "ok",
            "plan": "Pro",
            "reset_bank": 2,
            "reset_bank_text": "reset bank: 2 доступны",
            "windows": [
                {"key": "five_hour", "label": "5 часов", "used_percent": 40, "remaining_percent": 60, "reset_text": "reset in 2h"},
                {"key": "weekly", "label": "Неделя", "used_percent": 20, "remaining_percent": 80, "reset_text": "reset in 3d"}
            ]
        }
    ]
}, ensure_ascii=False))
