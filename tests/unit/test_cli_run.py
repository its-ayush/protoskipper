# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the protoskipper run CLI subcommand (P5.A.2)."""

from __future__ import annotations

from pathlib import Path


class TestCliRunSubcommand:
    def test_run_subcommand_registered(self) -> None:
        from protoskipper.cli import build_parser

        parser = build_parser()
        # Verify `run` is a valid subcommand by parsing it
        args = parser.parse_args(["run", "myscript.py"])
        assert args.command == "run"
        assert args.script == "myscript.py"
        assert args.allow_writes is False

    def test_run_allow_writes_flag(self) -> None:
        from protoskipper.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "--allow-writes", "myscript.py"])
        assert args.allow_writes is True

    def test_run_extra_args(self) -> None:
        from protoskipper.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "script.py", "arg1", "arg2"])
        assert args.extra_args == ["arg1", "arg2"]


class TestRunScript:
    def test_simple_script(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "hello.py"
        script.write_text("result = 1 + 1\n", encoding="utf-8")
        rc = run_script(script, extra_argv=[])
        assert rc == 0

    def test_missing_file(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        rc = run_script(tmp_path / "nonexistent.py", extra_argv=[])
        assert rc == 2

    def test_script_exception_returns_1(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "bad.py"
        script.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        rc = run_script(script, extra_argv=[])
        assert rc == 1

    def test_script_sys_exit_0(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "exit0.py"
        script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        rc = run_script(script, extra_argv=[])
        assert rc == 0

    def test_script_sys_exit_nonzero(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "exit3.py"
        script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
        rc = run_script(script, extra_argv=[])
        assert rc == 3

    def test_script_receives_argv(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "argv_check.py"
        out_file = tmp_path / "out.txt"
        script.write_text(
            f"out = open({str(out_file)!r}, 'w')\nout.write(','.join(argv))\nout.close()\n",
            encoding="utf-8",
        )
        rc = run_script(script, extra_argv=["a", "b", "c"])
        assert rc == 0
        assert out_file.read_text() == "a,b,c"

    def test_script_log_binding_available(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "use_log.py"
        script.write_text("log.info('hello from script')\n", encoding="utf-8")
        rc = run_script(script, extra_argv=[])
        assert rc == 0

    def test_allow_writes_flag_passed(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "check_writes.py"
        out_file = tmp_path / "out.txt"
        script.write_text(
            f"open({str(out_file)!r}, 'w').write(str(allow_writes))\n",
            encoding="utf-8",
        )
        rc = run_script(script, extra_argv=[], allow_writes=True)
        assert rc == 0
        assert out_file.read_text() == "True"

    def test_load_protocol_drivers_binding(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        script = tmp_path / "check_drivers.py"
        out_file = tmp_path / "out.txt"
        script.write_text(
            f"drivers = load_protocol_drivers()\n"
            f"open({str(out_file)!r}, 'w').write(type(drivers).__name__)\n",
            encoding="utf-8",
        )
        rc = run_script(script, extra_argv=[])
        assert rc == 0
        assert out_file.read_text() == "dict"

    def test_directory_path_returns_2(self, tmp_path: Path) -> None:
        from protoskipper.scripting import run_script

        rc = run_script(tmp_path, extra_argv=[])  # tmp_path is a directory
        assert rc == 2


class TestCliRunIntegration:
    def test_main_run_simple_script(self, tmp_path: Path) -> None:
        from protoskipper.cli import main

        script = tmp_path / "ok.py"
        script.write_text("pass\n", encoding="utf-8")
        rc = main(["run", str(script)])
        assert rc == 0

    def test_main_run_missing_file(self, tmp_path: Path) -> None:
        from protoskipper.cli import main

        rc = main(["run", str(tmp_path / "nope.py")])
        assert rc == 2
