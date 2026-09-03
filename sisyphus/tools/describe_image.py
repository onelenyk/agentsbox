#!/usr/bin/env python3
"""Describe image content using Gemini Flash via OpenRouter API. Outputs text to stdout."""
import base64
import json
import os
import sys
import urllib.request

def describe_image(image_path: str, api_key: str = None) -> str:
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("ERROR: Missing OPENROUTER_API_KEY")
        sys.exit(1)

    if not os.path.exists(image_path):
        print(f"ERROR: File not found: {image_path}")
        sys.exit(1)

    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
    mime_type = mime_map.get(ext, "image/png")

    payload = {
        "model": "google/gemini-flash-1.5",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this screenshot. Focus on UI elements, colors, text content, layout, and anything that looks incorrect or broken."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}}
            ]
        }]
    }

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"ERROR: {e}"

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python tools/describe_image.py IMAGE_PATH")
        print("Example: python tools/describe_image.py screenshot.png")
        sys.exit(0)

    description = describe_image(sys.argv[1])
    print(description)
