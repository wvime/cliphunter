from cliphunter.cli import main


def test_help_documents_run_and_clip(capsys):
    import pytest

    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "run" in help_text
    assert "clip" in help_text

    with pytest.raises(SystemExit) as clip_help:
        main(["clip", "--help"])
    assert clip_help.value.code == 0
    clip_text = capsys.readouterr().out
    assert "--canal" in clip_text
    assert "--ultimos" in clip_text

    with pytest.raises(SystemExit) as run_help:
        main(["run", "--help"])
    assert run_help.value.code == 0
    assert "--yes" in capsys.readouterr().out


def test_version_doctor_and_future_commands(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out
    code = main(["doctor"])
    text = capsys.readouterr().out
    assert "ffmpeg" in text
    assert "streamlink" in text
    assert code in (0, 1)
    assert main(["replay"]) == 2
    assert "replay" in capsys.readouterr().err
    assert main(["calibrate"]) == 2


def test_init_db_and_run_without_authorized_channels(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    database = tmp_path / "cliphunter.sqlite"
    config.write_text(
        "\n".join(
            [
                f'buffer_dir: "{(tmp_path / "buffer").as_posix()}"',
                f'database_path: "{database.as_posix()}"',
                "channels: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["init-db", "--config", str(config)]) == 0
    assert database.exists()
    assert main(["run", "--config", str(config), "--yes"]) == 2
    assert "authorized" in capsys.readouterr().err
