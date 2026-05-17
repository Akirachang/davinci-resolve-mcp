"""Hermetic tests for src/utils/whisperx_runner.py.

Mirrors the subprocess-mocking pattern used in tests/test_frame_extraction.py.
No `whisperx` binary or network is required.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils import whisperx_runner as wx


class TestCheckWhisperx(unittest.TestCase):
    def test_missing_returns_install_hint(self):
        with mock.patch("shutil.which", return_value=None):
            result = wx.check_whisperx()
        self.assertFalse(result["available"])
        self.assertIn("pip install whisperx", result["error"])

    def test_present_returns_version(self):
        fake = mock.Mock(returncode=0, stdout="whisperx, version 3.1.1\n", stderr="")
        with mock.patch("shutil.which", return_value="/usr/local/bin/whisperx"), \
             mock.patch("subprocess.run", return_value=fake):
            result = wx.check_whisperx()
        self.assertTrue(result["available"])
        self.assertEqual(result["path"], "/usr/local/bin/whisperx")
        self.assertEqual(result["version"], "3.1.1")

    def test_present_handles_stderr_version_output(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="whisperx 3.2.0\n")
        with mock.patch("shutil.which", return_value="/x/whisperx"), \
             mock.patch("subprocess.run", return_value=fake):
            result = wx.check_whisperx()
        self.assertTrue(result["available"])


class TestRunWhisperxCommandShape(unittest.TestCase):
    def _patch_run(self, captured):
        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")
        return fake_run

    def test_command_includes_required_flags(self):
        captured = {}
        with mock.patch("shutil.which", return_value="/x/whisperx"), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.makedirs"), \
             mock.patch("subprocess.run", side_effect=self._patch_run(captured)):
            ok, err, info = wx.run_whisperx(
                "/tmp/audio.wav", "/tmp/out",
                model="small", language="en",
            )
        self.assertTrue(ok, msg=err)
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "/x/whisperx")
        self.assertEqual(cmd[1], "/tmp/audio.wav")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "small")
        self.assertIn("--output_dir", cmd)
        self.assertEqual(cmd[cmd.index("--output_dir") + 1], "/tmp/out")
        self.assertIn("--output_format", cmd)
        self.assertEqual(cmd[cmd.index("--output_format") + 1], "all")
        self.assertIn("--compute_type", cmd)
        self.assertIn("--language", cmd)
        self.assertEqual(cmd[cmd.index("--language") + 1], "en")

    def test_auto_language_omits_language_flag(self):
        captured = {}
        with mock.patch("shutil.which", return_value="/x/whisperx"), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.makedirs"), \
             mock.patch("subprocess.run", side_effect=self._patch_run(captured)):
            wx.run_whisperx("/tmp/audio.wav", "/tmp/out", language="auto")
        self.assertNotIn("--language", captured["cmd"])

    def test_extra_args_appended_verbatim(self):
        captured = {}
        with mock.patch("shutil.which", return_value="/x/whisperx"), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.makedirs"), \
             mock.patch("subprocess.run", side_effect=self._patch_run(captured)):
            wx.run_whisperx("/tmp/audio.wav", "/tmp/out",
                            extra_args=["--vad_method", "silero", "--task", "transcribe"])
        cmd = captured["cmd"]
        self.assertEqual(cmd[-4:], ["--vad_method", "silero", "--task", "transcribe"])

    def test_missing_binary_returns_install_hint(self):
        with mock.patch("shutil.which", return_value=None):
            ok, err, info = wx.run_whisperx("/tmp/audio.wav", "/tmp/out")
        self.assertFalse(ok)
        self.assertIn("pip install whisperx", err)

    def test_missing_audio_returns_error(self):
        with mock.patch("shutil.which", return_value="/x/whisperx"), \
             mock.patch("os.path.isfile", return_value=False):
            ok, err, info = wx.run_whisperx("/missing/audio.wav", "/tmp/out")
        self.assertFalse(ok)
        self.assertIn("audio file does not exist", err)

    def test_nonzero_exit_returns_stderr_tail(self):
        fake = mock.Mock(returncode=1, stdout="",
                         stderr="something happened\nCUDA out of memory\n")
        with mock.patch("shutil.which", return_value="/x/whisperx"), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.makedirs"), \
             mock.patch("subprocess.run", return_value=fake):
            ok, err, info = wx.run_whisperx("/tmp/audio.wav", "/tmp/out")
        self.assertFalse(ok)
        self.assertIn("CUDA out of memory", err)
        self.assertIn("stderr_tail", info)


class TestFindOutputs(unittest.TestCase):
    def test_find_output_srt_picks_basename_match(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            preferred = Path(td) / "audio.srt"
            preferred.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
            other = Path(td) / "other.srt"
            other.write_text("1\n00:00:00,000 --> 00:00:01,000\nbye\n")
            found = wx.find_output_srt(td, "/somewhere/audio.wav")
        self.assertEqual(found, str(preferred))

    def test_find_output_srt_falls_back_to_any(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "different.srt").write_text("")
            found = wx.find_output_srt(td, "/somewhere/audio.wav")
        self.assertTrue(found.endswith("different.srt"))

    def test_find_output_srt_missing_dir(self):
        self.assertIsNone(wx.find_output_srt("/nonexistent/path", "/a.wav"))

    def test_find_output_json_picks_basename_match(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "audio.json").write_text("{}")
            found = wx.find_output_json(td, "/somewhere/audio.wav")
        self.assertTrue(found.endswith("audio.json"))


class TestCountSrtCues(unittest.TestCase):
    def test_counts_arrows(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False) as fh:
            fh.write(
                "1\n00:00:00,000 --> 00:00:01,000\nfirst\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nsecond\n\n"
            )
            path = fh.name
        try:
            self.assertEqual(wx.count_srt_cues(path), 2)
        finally:
            os.unlink(path)

    def test_missing_file_returns_zero(self):
        self.assertEqual(wx.count_srt_cues("/nope/missing.srt"), 0)


class TestDownmixToWhisperWav(unittest.TestCase):
    def test_missing_ffmpeg_returns_hint(self):
        with mock.patch("shutil.which", return_value=None):
            ok, err = wx.downmix_to_whisper_wav("/tmp/a.wav", "/tmp/b.wav")
        self.assertFalse(ok)
        self.assertIn("ffmpeg not found", err)

    def test_command_shape(self):
        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"RIFF....")
            return mock.Mock(returncode=0, stdout="", stderr="")

        import tempfile
        src = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        src.close()
        dst = src.name + ".out.wav"
        try:
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch("subprocess.run", side_effect=fake_run):
                ok, err = wx.downmix_to_whisper_wav(src.name, dst)
            self.assertTrue(ok, msg=err)
            cmd = captured["cmd"]
            self.assertIn("-ac", cmd)
            self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
            self.assertIn("-ar", cmd)
            self.assertEqual(cmd[cmd.index("-ar") + 1], "16000")
        finally:
            for p in (src.name, dst):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
