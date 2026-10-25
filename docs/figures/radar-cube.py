"""Frequency-domain radar data cube, in the documentation's ESPARGOS palette."""

from pathlib import Path

parts = ["""<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="500" viewBox="0 0 1000 500" role="img" aria-labelledby="title desc">
<title id="title">Radar data cube</title>
<desc id="desc">Channel measurements arranged by frequency, antenna position and measurement time. Frequency gives delay, antenna position gives direction, and changes across measurements give Doppler.</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#293135"/></marker></defs>
<rect width="1000" height="500" fill="white"/>
<g stroke="#0d4f1a" stroke-width="2" stroke-linejoin="round">
<path d="M230 340 L570 340 L570 150 L230 150 Z" fill="#d7f4e0"/>
<path d="M230 150 L350 65 L690 65 L570 150 Z" fill="#eef8f0"/>
<path d="M570 340 L690 255 L690 65 L570 150 Z" fill="#b6dfc1"/>
"""]
for i in range(1, 8):
    x = 230 + 340 * i / 8
    parts.append(f'<path d="M{x} 340 V150 L{x + 120} 65" fill="none" stroke="#86b997" stroke-width="1"/>')
for i in range(1, 5):
    y = 340 - 190 * i / 5
    parts.append(f'<path d="M230 {y} H570 L690 {y-85}" fill="none" stroke="#86b997" stroke-width="1"/>')
for i in range(1, 5):
    dx, dy = 120 * i / 5, -85 * i / 5
    parts.append(f'<path d="M{230+dx} {150+dy} H{570+dx} V{340+dy}" fill="none" stroke="#86b997" stroke-width="1"/>')
parts.append("""</g>
<g fill="none" stroke="#293135" stroke-width="2">
<path d="M230 370 H570"/>
<path d="M200 340 V150"/>
<path d="M600 355 L720 270"/>
</g>
<path d="M570 370 L558 364 L558 376 Z M200 150 L194 162 L206 162 Z M720 270 L706 273 L713 283 Z" fill="#293135"/>
<g font-family="Carlito, Calibri, Arial, sans-serif" font-size="22" fill="#293135">
<text x="400" y="408" text-anchor="middle">Frequency</text>
<text x="400" y="438" text-anchor="middle" fill="#007a7a">Delay / range</text>
<text x="176" y="224" text-anchor="end">Antenna</text>
<text x="176" y="253" text-anchor="end">position</text>
<text x="176" y="287" text-anchor="end" fill="#007a7a">Direction</text>
<text x="735" y="290">Measurement time</text>
<text x="735" y="319">(slow time)</text>
<text x="735" y="353" fill="#007a7a">Doppler / velocity</text>
</g></svg>""")
(Path(__file__).resolve().parents[1] / "_static" / "radar-cube.svg").write_text("\n".join(parts) + "\n")
