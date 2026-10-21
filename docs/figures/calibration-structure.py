"""Generate calibration overview diagrams in the ESPARGOS manual style."""

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "_static"
DARK, GRAY, GREEN, LIGHT, TEAL, PALE = "#293135", "#4a5550", "#37c964", "#d7f4e0", "#007a7a", "#dcefee"


class Diagram:
    def __init__(self, height, title, description):
        self.parts = [f"""<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>
<defs><marker id="arrow" markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="10" refX="10" refY="5" orient="auto-start-reverse"><path d="M0,0 L11,5 L0,10 L2.5,5 Z" fill="{DARK}"/></marker></defs>
<rect width="1000" height="{height}" fill="white"/><g font-family="Carlito, Calibri, Arial, sans-serif" font-size="19" fill="{DARK}">"""]

    def text(self, x, y, value, size=19, bold=False, anchor="middle", color=DARK):
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(value)}</text>')

    def rect(self, x, y, w, h, fill="white", stroke=DARK, radius=4):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')

    def box(self, x, y, w, title, lines, fill=LIGHT):
        h = 56 + 25 * len(lines)
        self.rect(x, y, w, h, fill)
        self.text(x + w / 2, y + 30, title, 23, True)
        for i, line in enumerate(lines):
            self.text(x + w / 2, y + 61 + i * 25, line)
        return h

    def heading(self, x, y, title):
        self.rect(x, y - 21, 5, 26, GREEN, GREEN, 0)
        self.text(x + 17, y, title, 23, True, "start")

    def line(self, path, arrow=False):
        self.parts.append(f'<path d="{path}" fill="none" stroke="{DARK}" stroke-width="2"' + (' marker-end="url(#arrow)"' if arrow else "") + "/>")

    def save(self, name):
        (OUT / name).write_text("\n".join(self.parts + ["</g></svg>"]) + "\n")


d = Diagram(520, "Relationships between calibration types", "F implies FR plus FT. FR plus FT implies FR, FT and R. The arrows indicate known information, not measurement steps.")
d.heading(25, 35, "What does each calibration tell us?")
d.box(300, 62, 400, "F", ["RX and TX phases, including", "their common TX–RX phase relation"])
d.line("M500,168 V208", True)
d.box(300, 220, 400, "FR + FT", ["RX and TX each aligned", "Common TX–RX offset unspecified"])
d.line("M500,326 V357 H180 V386", True)
d.line("M500,326 V386", True)
d.line("M500,357 H820 V386", True)
d.box(30, 398, 300, "FR", ["Relative RX phases", "Receive in known directions"])
d.box(350, 398, 300, "R", ["Relative TX/RX combination", "Beamforming from RX pilots"], PALE)
d.box(670, 398, 300, "FT", ["Relative TX phases", "Transmit in known directions"])
d.save("calibration-types.svg")


d = Diagram(
    590,
    "Examples of calibration coverage",
    "Four examples on two eight-antenna arrays: one shared receive calibration, a selected subset, two receive frequencies, and mixed receive and transmit roles. Each color denotes one group within an example, and gray denotes antennas outside the calibration.",
)


def aperture(x, y, groups, name):
    d.text(x + 82, y - 13, name, 19, True)
    for a, group in enumerate(groups):
        fill = {"a": LIGHT, "b": PALE, "-": "#f4f4f4"}[group]
        edge = {"a": "#227b3d", "b": TEAL, "-": "#d0d8d4"}[group]
        d.rect(x + (a % 4) * 43, y + (a // 4) * 36, 34, 27, fill, edge)


def example(x, y, title, left, right, labels):
    d.heading(x, y, title)
    aperture(x + 30, y + 58, left, "Array A")
    aperture(x + 255, y + 58, right, "Array B")
    for i, (label, color) in enumerate(labels):
        if color:
            d.rect(x + 30, y + 145 + i * 29, 15, 15, color, DARK, 2)
        d.text(x + 56, y + 158 + i * 29, label, 19, anchor="start", color=GRAY)


example(25, 35, "One group across both arrays", "aaaaaaaa", "aaaaaaaa", [("FR at 2437 MHz", LIGHT), ("One shared phase reference", None)])
example(525, 35, "Only a selected subset", "a-a--a--", "--------", [("FR for three antennas", LIGHT), ("Gray antennas are outside the group", None)])
example(25, 315, "Different receive frequencies", "aaaabbbb", "aaaabbbb", [("FR at 2412 MHz", LIGHT), ("FR at 2437 MHz", PALE)])
example(525, 315, "Mixed receive and transmit roles", "bbbbbbbb", "aaaaaaaa", [("FR on Array B", LIGHT), ("FT on Array A", PALE)])
d.text(500, 575, "Each colored group has its own frequency, sample rate and phase reference.", color=GRAY)
d.save("calibration-coverage.svg")


d = Diagram(
    710,
    "Python calibration objects",
    "A Calibration contains zero or more CalibrationGroups. Each group contains one or more AntennaCorrections and a reference ID. Each correction contains phase, delay and a CalibrationState. The state identifies an Antenna and the hardware configuration for which the correction is valid.",
)
d.heading(25, 35, "The Python objects")
d.box(35, 70, 350, "Calibration", ["groups", "diagnostics"])
d.line("M385,123 H595", True)
d.text(490, 107, "groups: 0…N", 18, color=GRAY)
d.box(610, 70, 350, "CalibrationGroup", ["corrections", "reference_id"])
d.line("M785,176 V238", True)
d.text(770, 214, "corrections: 1…N", 18, anchor="end", color=GRAY)
d.box(610, 250, 350, "AntennaCorrection", ["phase_rad, delay_s", "state"])
d.line("M610,303 H400", True)
d.text(505, 287, "state: 1", 18, color=GRAY)
d.box(35, 250, 350, "CalibrationState", ["direction: rx or tx", "frequency_hz, sample_rate_hz", "boot_id, epoch, settings", "antenna"], "#edfbf2")
d.line("M210,406 V468", True)
d.text(230, 445, "antenna: 1", 18, anchor="start", color=GRAY)
d.box(35, 480, 350, "Antenna", ["host", "antid: hardware ID 0–7"], "white")
d.text(610, 420, "Within one group:", 21, True, "start")
d.text(610, 450, "One RX/TX direction", anchor="start", color=GRAY)
d.text(610, 479, "One frequency and sample rate", anchor="start", color=GRAY)
d.text(610, 508, "One shared phase reference", anchor="start", color=GRAY)
d.text(610, 560, "Across groups:", 21, True, "start")
d.text(610, 590, "Scopes and references may differ", anchor="start", color=GRAY)
d.text(500, 653, "correction(state) returns a matching correction or raises KeyError.", color=GRAY)
d.text(500, 682, "select(...) retains the phase reference. merge(...) does not align independent groups.", color=GRAY)
d.save("calibration-objects.svg")
