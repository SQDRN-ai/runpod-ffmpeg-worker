import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_handler_module():
    fake_runpod = types.SimpleNamespace(
        serverless=types.SimpleNamespace(start=lambda _config: None)
    )
    sys.modules.setdefault("runpod", fake_runpod)
    spec = importlib.util.spec_from_file_location(
        "birthday_render_handler", os.path.join(REPO_ROOT, "handler.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class SlideshowIntegrationTest(unittest.TestCase):
    def test_two_image_slideshow_with_text_and_thumbnail(self):
        handler = load_handler_module()
        with tempfile.TemporaryDirectory(prefix="birthday-slideshow-test-") as directory:
            image_one = os.path.join(directory, "one.png")
            image_two = os.path.join(directory, "two.png")
            music = os.path.join(directory, "music.mp3")
            uploaded_video = os.path.join(directory, "uploaded.mp4")
            uploaded_thumbnail = os.path.join(directory, "uploaded.jpg")

            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x7A1FA2:s=640x360:d=1",
                "-frames:v", "1", image_one,
            ], check=True, capture_output=True)
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0xFFB300:s=640x360:d=1",
                "-frames:v", "1", image_two,
            ], check=True, capture_output=True)
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:a", "libmp3lame", music,
            ], check=True, capture_output=True)

            source_files = {
                "assets/one.png": image_one,
                "assets/two.png": image_two,
                "music/test.mp3": music,
            }

            def fake_download(key, destination):
                shutil.copyfile(source_files[key], destination)

            def fake_upload(source, key):
                destination = uploaded_thumbnail if key.endswith(".jpg") else uploaded_video
                shutil.copyfile(source, destination)
                return {"bucket": "test", "key": key, "url": f"https://example.invalid/{key}"}

            handler.download_from_r2 = fake_download
            handler.upload_to_r2 = fake_upload

            result = handler.handler({"input": {
                "mode": "render_slideshow",
                "jobId": "age-party-25",
                "music_key": "music/test.mp3",
                "image_keys": ["assets/one.png", "assets/two.png"],
                "out_key": "jobs/age-party-25/final.mp4",
                "render": {
                    "canvas": {"width": 640, "height": 360},
                    "slideshow": {
                        "fps": 15,
                        "image_duration_seconds": 2,
                        "transition_seconds": 0.25,
                        "animations": ["zoom_in", "pan_right"],
                        "transitions": ["fade"],
                        "seed": 25,
                    },
                    "video": {"preset": "ultrafast", "crf": 28},
                    "text_events": [{
                        "text": "25TH",
                        "start_seconds": 0.3,
                        "end_seconds": 2.4,
                        "size": 96,
                        "animation": "pop",
                    }],
                    "thumbnail": {
                        "enabled": True,
                        "out_key": "jobs/age-party-25/thumb.jpg",
                        "size": {"width": 640, "height": 360},
                        "name_text": {
                            "text": "25TH",
                            "fontsize": 96,
                            "borderw": 4,
                            "bordercolor": "black",
                        },
                    },
                },
            }})

            self.assertEqual(result.get("status"), "ok", result)
            self.assertEqual(result.get("mode"), "render_slideshow")
            self.assertEqual(result.get("image_count"), 2)
            self.assertTrue(result.get("generated_text_events_used"))
            self.assertTrue(os.path.exists(uploaded_video))
            self.assertTrue(os.path.exists(uploaded_thumbnail))


if __name__ == "__main__":
    unittest.main()
