from pathlib import Path

from src.resume import resolve_resume_path


def test_resolve_resume_path_prefers_final_file(tmp_path):
    other = tmp_path / "Aditya_Pandey_Resume092026.pdf"
    final = tmp_path / "Aditya_Pandey_Resume_Final.pdf"
    other.write_bytes(b"pdf")
    final.write_bytes(b"pdf")

    assert resolve_resume_path(tmp_path) == final


def test_resolve_resume_path_uses_resume_name_when_final_missing(tmp_path):
    resume = tmp_path / "my_resume.pdf"
    resume.write_bytes(b"pdf")

    assert resolve_resume_path(tmp_path) == resume
