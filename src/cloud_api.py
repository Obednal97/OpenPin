"""Plaud Cloud API client - download recordings synced to the cloud."""

import os
from dataclasses import dataclass
from pathlib import Path

import requests


@dataclass
class PlaudRecording:
    file_id: str
    filename: str
    duration: int  # seconds
    file_size: int
    start_time: str
    device_sn: str
    scene: int


class PlaudCloudClient:
    """Client for Plaud's cloud API to download already-synced recordings."""

    def __init__(self, token: str, api_domain: str = "https://api-euc1.plaud.ai"):
        self.token = token
        self.api_domain = api_domain
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "app-platform": "desktop",
                "app-language": "en",
            }
        )

    def get_user_info(self) -> dict:
        """Get current user info."""
        r = self.session.get(f"{self.api_domain}/user/me")
        r.raise_for_status()
        return r.json()

    def get_devices(self) -> list[dict]:
        """List bound devices."""
        r = self.session.get(f"{self.api_domain}/device/list")
        r.raise_for_status()
        data = r.json()
        return data.get("data_devices", [])

    def get_recordings(self, limit: int = 100, skip: int = 0, is_trash: bool = False) -> list[PlaudRecording]:
        """Get list of recordings from cloud."""
        params = {
            "skip": skip,
            "limit": limit,
            "is_trash": 1 if is_trash else 0,
            "sort_by": "edit_time",
            "is_desc": "true",
        }
        r = self.session.get(f"{self.api_domain}/file/simple/web", params=params)
        r.raise_for_status()
        data = r.json()

        recordings = []
        for item in data.get("data_file_list", data.get("data_list", [])):
            duration = item.get("duration", 0)
            if duration > 1000:
                duration = duration // 1000  # Convert ms to seconds
            recordings.append(
                PlaudRecording(
                    file_id=item.get("id", item.get("file_id", "")),
                    filename=item.get("filename", "unknown"),
                    duration=duration,
                    file_size=item.get("filesize", 0),
                    start_time=item.get("start_time", ""),
                    device_sn=item.get("serial_number", item.get("device_sn", "")),
                    scene=item.get("scene", 0),
                )
            )
        return recordings

    def get_download_url(self, file_id: str, opus: bool = True) -> str:
        """Get temporary S3 presigned download URL."""
        params = {"is_opus": 1 if opus else 0}
        r = self.session.get(f"{self.api_domain}/file/temp-url/{file_id}", params=params)
        r.raise_for_status()
        data = r.json()
        if opus:
            return data.get("temp_url_opus", data.get("temp_url", ""))
        return data.get("temp_url", "")

    def download_recording(self, file_id: str, output_path: str, opus: bool = True) -> bool:
        """Download a recording to a file."""
        url = self.get_download_url(file_id, opus=opus)
        if not url:
            print(f"  Could not get download URL for {file_id}")
            return False

        r = requests.get(url, stream=True)
        r.raise_for_status()

        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True

    def download_all(self, output_dir: str = "cloud_recordings", opus: bool = True) -> list[Path]:
        """Download all recordings from the cloud."""
        recordings = self.get_recordings(limit=9999)
        if not recordings:
            print("No recordings found in cloud")
            return []

        os.makedirs(output_dir, exist_ok=True)
        print(f"Found {len(recordings)} recording(s) in cloud")

        saved = []
        for i, rec in enumerate(recordings):
            ext = "opus" if opus else "wav"
            safe_name = rec.filename.replace("/", "-").replace(":", "-")
            filename = f"{safe_name}.{ext}"
            filepath = Path(output_dir) / filename

            if filepath.exists():
                print(f"  [{i + 1}/{len(recordings)}] Skipping (exists): {filename}")
                saved.append(filepath)
                continue

            print(f"  [{i + 1}/{len(recordings)}] Downloading: {filename} ({rec.duration}s)")
            try:
                if self.download_recording(rec.file_id, str(filepath), opus=opus):
                    saved.append(filepath)
                    print(f"    Saved: {filepath}")
            except Exception as e:
                print(f"    Error: {e}")

        return saved
