"""Two independent signals steered by one ESPARGOS array."""

from pathlib import Path
import math

parts = ["""<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="535" viewBox="0 0 1000 535" role="img" aria-labelledby="title desc">
<title id="title">Two waveforms, two transmit beams</title>
<desc id="desc">A two by four ESPARGOS array steers a sensing chirp to the left and a communications signal to the right. Every transmitting sensor contributes to both beams.</desc>
<rect width="1000" height="535" fill="white"/>
<g font-family="Carlito, Calibri, Arial, sans-serif" font-size="22" fill="#293135">
<text x="230" y="390" text-anchor="middle" font-weight="700">Sensing</text>
<text x="770" y="390" text-anchor="middle" font-weight="700">Communications</text>
<text x="230" y="419" text-anchor="middle" font-size="19">Linear chirp</text>
<text x="770" y="419" text-anchor="middle" font-size="19">QPSK</text>
<rect x="90" y="437" width="280" height="70" rx="4" fill="white" stroke="#4a5550"/>
<rect x="630" y="437" width="280" height="70" rx="4" fill="white" stroke="#4a5550"/>
<path d="M104,472 H356 M644,472 H896" stroke="#d8dedb"/>
"""]
for start, color, waveform in [(104, "#219e49", "chirp"), (644, "#007a7a", "qpsk")]:
    points = []
    for i in range(253):
        t = i / 252
        value = math.sin(2 * math.pi * (t + 9 * t * t)) if waveform == "chirp" else math.sin(2 * math.pi * 10 * t + (int(t * 8) % 4) * math.pi / 2)
        points.append(f"{start+i},{472-23*value:.2f}")
    parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"/>')
# Match manual.tex's board/patch styles and 3.0 cm by 1.55 cm geometry.
parts.append('<rect x="352" y="24" width="296" height="153" rx="3" fill="#0d4f1a" stroke="#082f10" stroke-width="3"/>')
for row in range(2):
    for col in range(4):
        x, y = 396.4 + 69.07 * col, 65.97 + 69.07 * row
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="22.2" fill="#b49024" stroke="#e6d49e" stroke-width="4"/>')
# Define each lobe around its centerline, then rotate both together.
beam = "M0,0 C-8,90 -52,250 -42,310 C-32,360 32,360 42,310 C52,250 8,90 0,0Z"
for angle, color, opacity in [(51.34, "#37c964", 0.18), (-51.34, "#007a7a", 0.14)]:
    parts.append(f"""<g transform="translate(500 100.5) rotate({angle})">
<path d="{beam}" fill="{color}" fill-opacity="{opacity}" stroke="{color}" stroke-width="2"/>
<path d="M0,0 L0,340" stroke="#4a5550" stroke-width="1.5" stroke-dasharray="7 6"/>
</g>""")
parts.append("</g></svg>")
(Path(__file__).resolve().parents[1] / "_static" / "iq-beams.svg").write_text("\n".join(parts) + "\n")
