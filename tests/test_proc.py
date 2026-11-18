import asyncio
import sys

from cliphunter.procutil import terminate_process


async def test_terminate_process_stops_a_python_sleep():
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await terminate_process(proc, timeout=2)
    assert proc.returncode is not None
