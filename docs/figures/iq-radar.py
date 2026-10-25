"""Monostatic radar: one ESPARGOS array transmits and receives."""

from pathlib import Path
import xml.etree.ElementTree as ET
import math

# Preserve the original person outline from the paper as a vector asset.
ET.register_namespace("", "http://www.w3.org/2000/svg")
person = ET.parse(Path(__file__).with_name("radar-person.svg")).getroot()
person.set("x", "768")
person.set("y", "57")

# Standard isometric axes: horizontal directions at ±30°, vertical upright.
# Use equal scale along all three axes, without extra side-view compression.
scale = 0.48
horizontal = math.cos(math.radians(30)) * scale
vertical = -math.sin(math.radians(30)) * scale
x0, y0 = 110, 156
width, height, thickness = 296, 153, 12


def project(x, y, depth=0):
    return (x0 + horizontal * (x - depth), y0 + vertical * (x + depth) + scale * y)


def face(vertices, color):
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in vertices)
    return f'<polygon points="{points}" fill="{color}" stroke="#082f10" stroke-width="1.5" stroke-linejoin="round"/>'


array = []
array.append(face([project(0, 0), project(0, 0, thickness), project(0, height, thickness), project(0, height)], "#082f10"))
array.append(face([project(0, 0), project(0, 0, thickness), project(width, 0, thickness), project(width, 0)], "#093d16"))
# Draw all three faces in screen coordinates with the same outline width.
# Mixing a transformed rounded rectangle with sharp side faces leaves spikes.
array.append(face([project(0, 0), project(width, 0), project(width, height), project(0, height)], "#0d4f1a"))
array.append(f'<g transform="matrix({horizontal:.8f} {vertical} 0 {scale} {x0} {y0})">')
for row in range(2):
    for col in range(4):
        x, y = 44.4 + 69.07 * col, 41.97 + 69.07 * row
        array.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="22.2" fill="#b49024" stroke="#e6d49e" stroke-width="4"/>')
array.append("</g>")

svg = """<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="350" viewBox="0 0 1000 350" role="img" aria-labelledby="title desc">
<title id="title">An ESPARGOS array detects a person</title>
<desc id="desc">An isometric view of a single ESPARGOS array. Some antennas transmit toward a person, while other antennas on the same array receive the reflected signal.</desc>
<!-- Keep the shaft end inside the arrowhead, away from its narrow tip. -->
<defs>
  <marker id="tx" viewBox="-1 -1 14 12" markerWidth="14" markerHeight="12" refX="7" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L12 5 L0 10Z" fill="#219e49"/></marker>
  <marker id="rx" viewBox="-1 -1 14 12" markerWidth="14" markerHeight="12" refX="7" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L12 5 L0 10Z" fill="#007a7a"/></marker>
</defs>
<rect width="1000" height="350" fill="white"/>
<g font-family="Carlito, Calibri, Arial, sans-serif" font-size="22" fill="#293135">
__ARRAY__
<text x="176" y="317" text-anchor="middle">ESPARGOS array</text>
<text x="176" y="341" text-anchor="middle" font-size="17" fill="#66746d">TX and RX</text>
<!-- Separate arrows make the return path clear without implying two stations. -->
<path d="M238 115 Q468 52 765 142" fill="none" stroke="#219e49" stroke-width="3" stroke-linecap="round" marker-end="url(#tx)"/>
<path d="M765 169 Q468 249 238 145" fill="none" stroke="#007a7a" stroke-width="3" stroke-linecap="round" marker-end="url(#rx)"/>
<text x="472" y="73" text-anchor="middle" fill="#21843f">Transmitted signal</text>
<text x="472" y="254" text-anchor="middle" fill="#007a7a">Reflected signal</text>
__PERSON__
<text x="803" y="317" text-anchor="middle">Person</text>
</g>
</svg>
"""
(Path(__file__).resolve().parents[1] / "_static" / "iq-radar.svg").write_text(svg.replace("__ARRAY__", "\n".join(array)).replace("__PERSON__", ET.tostring(person, encoding="unicode")))
