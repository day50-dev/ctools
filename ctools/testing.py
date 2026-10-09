"""A tiny in-process runner for the ctools command CLIs.

The tools are plain ``main(argv)`` argparse programs (see ``ctools.cli``),
and this mirrors the small subset of ``typer.testing.CliRunner`` the test
suite has always used:

    runner = Runner()
    result = runner.invoke(app, ["--long", "opencode/"], input="...")
    result.exit_code    # 0 on success, 2 on a usage error, 1 otherwise
    result.stdout       # standard output
    result.output       # standard output + standard error, combined

It exists so tests can keep invoking commands by the module-level ``app``
alias the way they did under Typer; nothing production imports this module.
"""

import io
import sys
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Result:
    """The outcome of one in-process invocation."""
    exit_code: int
    stdout: str
    stderr: str = field(default="")

    @property
    def output(self) -> str:
        """stdout + stderr combined (the CliRunner.result.output analogue)."""
        return self.stdout + self.stderr


class Runner:
    """Invoke a ctools ``main``/``app`` in-process with captured streams."""

    def invoke(self, app, args: Optional[List[str]] = None,
               input: Optional[str] = None) -> Result:
        args = list(args or [])
        old_argv, old_stdout, old_stderr, old_stdin = \
            sys.argv, sys.stdout, sys.stderr, sys.stdin
        out, err = io.StringIO(), io.StringIO()
        try:
            sys.argv = ["app"] + args
            sys.stdout, sys.stderr = out, err
            if input is not None:
                sys.stdin = io.StringIO(input)
            try:
                code = app()
            except SystemExit as e:
                code = e.code
            except Exception as e:  # a crash is a failure, not a test error
                err.write(f"{type(e).__name__}: {e}\n")
                code = 1
        finally:
            sys.argv, sys.stdout, sys.stderr, sys.stdin = \
                old_argv, old_stdout, old_stderr, old_stdin
        code = code if isinstance(code, int) else (0 if code is None else 1)
        return Result(exit_code=code, stdout=out.getvalue(), stderr=err.getvalue())
