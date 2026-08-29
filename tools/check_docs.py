#!/usr/bin/env python3
"""Check the documentation's factual claims against the code.

Documentation rots quietly. Translating the docs into Chinese turned up four claims
that had already drifted -- a missing endpoint, an omitted peripheral, a table still
calling TIM a stub after TIM2 was modelled, and library modules absent from the layout.
None of those were caught by reading; they were caught by comparing against the source.
So compare mechanically, and keep doing it.

What is checked:
  1. every tool named in a README exists
  2. every test in run_tests.sh is documented in both READMEs
  3. every internal .md link resolves
  4. the English/Chinese pairs have matching section structure
  5. memory-map addresses match the model's #defines
  6. firmware file:line references point at what the docs say they do

Run it after touching docs or renaming anything:

    python3 tools/check_docs.py

One caution learned while writing this. An early version compared firmware constants
with a regex that grabbed the first number on the line, so `key_debounce_10ms = 20 / 10`
read as 20 and the check reported the docs wrong when they said 2. The docs were right
and the checker was broken. A checker that cries wolf gets ignored, so anything it
cannot verify unambiguously is left out rather than guessed at.
"""

import pathlib
import re
import sys

SIM = pathlib.Path(__file__).resolve().parent.parent
FW = pathlib.Path("/root/uvk5-port/uvk5-sat/App")

PAIRS = [
    ("README.md", "README.zh-CN.md"),
    ("AGENTS.md", "AGENTS.zh-CN.md"),
    ("docs/reverse-proxy.md", "docs/reverse-proxy.zh-CN.md"),
]

# Addresses the READMEs state, against the model's own #defines.
MEMORY_MAP = {
    "PY32_FLASH_BASE": "0x08000000",
    "PY32_SRAM_BASE": "0x20000000",
    "PY32_RCC_BASE": "0x40021000",
    "PY32_SPI1_BASE": "0x40013000",
    "PY32_SPI2_BASE": "0x40003800",
    "PY32_ADC1_BASE": "0x40012400",
    "PY32_APP_OFFSET": "0x2800",
}

# A documented file:line and a word that must appear near it. The window is a few
# lines wide on purpose: a reference drifting by a line or two is still useful, and
# failing on that would make the check noise.
LINE_REFS = {
    ("app/app.c", 1697): "CheckRadioInterrupts",
    ("app/app.c", 910): "REG_0C",
    ("app/app.c", 1417): "REG_0C",
    ("app/app.c", 915): "uint16_t",
    ("app/app.c", 1027): "SquelchLost",
    ("app/app.c", 482): "StartListening",
    ("app/app.c", 1374): "BATTERY_SAVE",
    ("app/app.c", 1700): "TRANSMIT",
    ("driver/gpio.h", 31): "PTT",
    ("driver/gpio.h", 34): "AUDIO_PATH",
    ("driver/bk4819.c", 743): "SetFrequency",
    ("settings.c", 263): "KEY_1_SHORT",
    ("settings.c", 423): "mic_bar",
    ("ui/main.c", 2370): "Rx",
    ("app/menu.c", 2311): "Direction",
    ("app/menu.c", 1826): "gMenuListCount",
}

WINDOW = 4

problems = []


def fail(msg):
    problems.append(msg)
    print(f"  FAIL  {msg}")


def resolve_fw(name):
    name = name.replace("App/", "")
    for cand in (FW / name, FW / "app" / name, FW / "driver" / name,
                 FW / "helper" / name, FW / "ui" / name):
        if cand.exists():
            return cand
    return None


def check_tools_exist():
    print("tools named in a README must exist")
    for doc in ("README.md", "README.zh-CN.md"):
        text = (SIM / doc).read_text()
        for tool in sorted(set(re.findall(r"tools/([a-z0-9_]+\.(?:py|sh))", text))):
            if not (SIM / "tools" / tool).exists():
                fail(f"{doc} names tools/{tool}, which does not exist")


def check_tests_documented():
    print("every test in run_tests.sh must be documented")
    runner = (SIM / "tools" / "run_tests.sh").read_text()
    in_runner = set(re.findall(r"tools/([a-z0-9_]+\.(?:py|sh))", runner))
    for doc in ("README.md", "README.zh-CN.md"):
        text = (SIM / doc).read_text()
        for tool in sorted(in_runner):
            if tool not in text:
                fail(f"{doc} does not mention {tool}, which run_tests.sh runs")


def check_links():
    print("internal .md links must resolve")
    for doc in [d for pair in PAIRS for d in pair]:
        path = SIM / doc
        for target in re.findall(r"\]\(([^)]+\.md)\)", path.read_text()):
            if target.startswith("http"):
                continue
            if not (path.parent / target).exists():
                fail(f"{doc} links to {target}, which does not exist")


def check_pairs():
    print("translation pairs must have matching structure")
    for en_name, zh_name in PAIRS:
        en = re.findall(r"^(#+) (.+)$", (SIM / en_name).read_text(), re.M)
        zh = re.findall(r"^(#+) (.+)$", (SIM / zh_name).read_text(), re.M)
        if len(en) != len(zh):
            fail(f"{en_name} has {len(en)} headings, {zh_name} has {len(zh)}")
            continue
        for i, ((en_lvl, en_txt), (zh_lvl, _)) in enumerate(zip(en, zh)):
            if en_lvl != zh_lvl:
                fail(f"{zh_name} heading {i + 1} is at a different depth than "
                     f"{en_name}'s ({en_txt!r})")


def check_memory_map():
    print("memory-map addresses must match the model")
    model = (SIM / "qemu" / "py32f071.c").read_text()
    for sym, documented in MEMORY_MAP.items():
        m = re.search(rf"#define {sym}\s+(\S+)", model)
        if not m:
            fail(f"{sym} is documented but not defined in the model")
        elif documented.lower() not in m.group(1).lower():
            fail(f"docs say {sym} is {documented}, model says {m.group(1)}")


def check_line_refs():
    print("firmware file:line references must point at what the docs claim")
    for (name, line), needle in sorted(LINE_REFS.items()):
        path = resolve_fw(name)
        if path is None:
            fail(f"{name} is referenced but not found in the firmware tree")
            continue
        lines = path.read_text().splitlines()
        if line > len(lines):
            fail(f"{name}:{line} is past the end of the file ({len(lines)} lines)")
            continue
        window = "\n".join(lines[max(0, line - WINDOW - 1):line + WINDOW])
        if needle.lower() not in window.lower():
            fail(f"{name}:{line} has no {needle!r} nearby -- "
                 f"the reference has drifted")


def main():
    for check in (check_tools_exist, check_tests_documented, check_links,
                  check_pairs, check_memory_map, check_line_refs):
        check()

    print()
    if problems:
        print(f"{len(problems)} documentation problem(s)")
        return 1
    print("documentation matches the code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
