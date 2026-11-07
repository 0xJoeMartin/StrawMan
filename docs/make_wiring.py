"""Generates docs/wiring-diagram.svg with the boards drawn to scale.

    python3 docs/make_wiring.py docs/wiring-diagram.svg
    /Applications/Inkscape.app/Contents/MacOS/inkscape docs/wiring-diagram.svg \
        --export-type=png --export-filename=docs/wiring-diagram.png --export-width=1800

Board geometry (mm):
  Raspberry Pi 5   85 x 56, holes 3.5 / 61.5 x 3.5 / 52.5, GPIO header centred
                   on the holes (pin 1 at x 8.37), even pins on the board edge.
                   Ports: USB 2 stack y 9, USB 3 stack y 26.9, Ethernet y 45.8
                   (from the GPIO edge); USB-C x 11.2, micro HDMI x 25.8 / 39.2.
  AI HAT+          65 x 56.5, same holes, stacking header passes GPIO through.
  PCA9685          62.5 x 25.4 (Adafruit 815 layout): 6-pin control header at
                   both ends (GND OE SCL SDA VCC V+), 16 servo outputs in 4
                   groups of 4, 2-pin power terminal in the middle of the edge.
  HLK-LD2450       44 x 15, 4-pin JST ZH (1.5 mm) socket.
The Pi is drawn turned 90 deg clockwise (header on the right, pin 1 at the top)
and the PCA9685 turned 180 deg so its far-end header faces the Pi; both ends'
headers are the same, and this way the four wires don't cross.
"""
import sys

S = 6.0                      # px per mm
W, H = 1800, 1360
out = []
def add(s): out.append(s)

def r(v): return f"{v:.1f}"

def rect(x, y, w, h, fill, stroke="none", sw=0, rx=0, extra=""):
    add(f'<rect x="{r(x)}" y="{r(y)}" width="{r(w)}" height="{r(h)}" rx="{r(rx)}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>')

def rect2(x1, y1, x2, y2, *a, **k):
    rect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1), *a, **k)

def circle(x, y, rad, fill, stroke="none", sw=0, extra=""):
    add(f'<circle cx="{r(x)}" cy="{r(y)}" r="{r(rad)}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>')

def text(x, y, s, size=16, fill="#212529", weight=400, anchor="start", extra=""):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    add(f'<text x="{r(x)}" y="{r(y)}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}" {extra}>{s}</text>')

def wire(points, color, width=5, casing=True, rad=8):
    """Orthogonal polyline with rounded corners, white casing under it."""
    d = f"M{r(points[0][0])},{r(points[0][1])}"
    for i in range(1, len(points) - 1):
        (x0, y0), (x1, y1), (x2, y2) = points[i - 1], points[i], points[i + 1]
        def toward(ax, ay, bx, by, dist):
            L = max(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5, 1e-6)
            dist = min(dist, L / 2)
            return ax + (bx - ax) * dist / L, ay + (by - ay) * dist / L
        p1 = toward(x1, y1, x0, y0, rad)
        p2 = toward(x1, y1, x2, y2, rad)
        d += f" L{r(p1[0])},{r(p1[1])} Q{r(x1)},{r(y1)} {r(p2[0])},{r(p2[1])}"
    d += f" L{r(points[-1][0])},{r(points[-1][1])}"
    if casing:
        add(f'<path d="{d}" fill="none" stroke="#fbf8f3" stroke-width="{width + 4}" stroke-linecap="round" stroke-linejoin="round"/>')
    add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')

C = dict(gnd="#212529", scl="#e0b000", sda="#1971c2", vcc="#f08c00", v5="#e03131",
         rx="#0c8599", tx="#9c36b5", usb="#868e96")
GOLD = "#d9b44a"

# --------------------------------------------------------------- canvas
add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    f'font-family="Helvetica Neue, Helvetica, Arial, sans-serif">')
rect(0, 0, W, H, "#fbf8f3")
text(60, 62, "StrawMan — how it's wired", 40, weight=700)
text(60, 95, "Raspberry Pi 5 + AI HAT+ · PCA9685 servo driver · HLK-LD2450 radar · USB camera, microphone and speaker",
     18, "#6c757d")

# --------------------------------------------------------------- Raspberry Pi 5
PX0, PY0 = 90, 200
def P(x, y):              # Pi mm (x along 85, y from GPIO edge) -> screen
    return PX0 + (56 - y) * S, PY0 + x * S
def prect(x1, y1, x2, y2, *a, **k):
    ax, ay = P(x1, y1); bx, by = P(x2, y2)
    rect2(ax, ay, bx, by, *a, **k)

text(PX0, 148, "Raspberry Pi 5", 28, weight=700)
text(PX0, 172, "85 × 56 mm · AI HAT+ on top (dashed)", 15, "#495057")

prect(0, 0, 85, 56, "#2b7a4b", "#1d5a37", 2, rx=3 * S)
for hx, hy in [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)]:
    cx, cy = P(hx, hy)
    circle(cx, cy, 3 * S, "#c9a74a")
    circle(cx, cy, 1.35 * S, "#fbf8f3")
# chips (under the HAT)
prect(22, 21, 37, 36, "#b8bec4", "#868e96", 1, rx=4)
cx, cy = P(29.5, 28.5); text(cx, cy + 5, "BCM2712", 12, "#495057", 700, "middle")
prect(8, 21, 18, 35, "#1f2326", rx=3)
prect(52, 18, 60, 26, "#1f2326", rx=3)
cx, cy = P(56, 22); text(cx, cy + 4, "RP1", 11, "#adb5bd", 700, "middle")
# FFC connectors: PCIe (left edge), CAM/DISP x2
prect(0.6, 20, 3.6, 32, "#f1f3f5", "#adb5bd", 1)
prect(50, 33.5, 52.8, 46.5, "#f1f3f5", "#adb5bd", 1)
prect(54.6, 33.5, 57.4, 46.5, "#f1f3f5", "#adb5bd", 1)
# ports: USB-C + micro HDMI on the bottom edge
prect(6.7, 49.5, 15.7, 57.2, "#adb5bd", "#495057", 1.5, rx=4)
prect(22.05, 50.5, 29.55, 57.0, "#adb5bd", "#495057", 1.5, rx=2)
prect(35.45, 50.5, 42.95, 57.0, "#adb5bd", "#495057", 1.5, rx=2)
# USB 2 stack (black), USB 3 stack (blue), Ethernet
def usb(yc, tongue):
    prect(66.5, yc - 6.6, 87.2, yc + 6.6, "#ced4da", "#495057", 1.5, rx=2)
    prect(84.8, yc - 5.4, 87.2, yc + 5.4, tongue)
usb(9.0, "#212529")
usb(26.9, "#1c7ed6")
prect(63.5, 37.8, 87.6, 53.8, "#ced4da", "#495057", 1.5, rx=2)
prect(84.8, 41.8, 87.6, 49.8, "#495057")

# AI HAT+ (translucent, dashed) + its ribbon to the Pi's PCIe connector
prect(0, 0, 65, 56.5, "#163d26", "#e9f7ef", 2, rx=3 * S, extra='fill-opacity="0.55" stroke-dasharray="10 7"')
prect(12, 32, 24, 44, "#111", "#495057", 1, rx=3)
cx, cy = P(18, 38); text(cx, cy + 5, "Hailo-8", 13, "#e9ecef", 700, "middle")
x1, y1 = P(0, 31); x2, y2 = P(0, 21)
add(f'<path d="M{r(x1)},{r(PY0)} C{r(x1)},{r(PY0-26)} {r(x2)},{r(PY0-26)} {r(x2)},{r(PY0)}" '
    f'fill="none" stroke="#e8a33d" stroke-width="10" stroke-linecap="butt" opacity="0.9"/>')
text(x2 + 14, PY0 - 6, "PCIe ribbon", 13, "#8a5a12")
cx, _ = P(0, 28)
text(cx, P(40, 0)[1], "AI HAT+ (Hailo-8)", 17, "#fff", 700, "middle")
text(cx, P(40, 0)[1] + 22, "sits on top — its header", 14, "#d3f0de", anchor="middle")
text(cx, P(40, 0)[1] + 40, "passes the GPIO pins through", 14, "#d3f0de", anchor="middle")

# GPIO header (drawn over the HAT: the pins come up through it)
prect(7.1, 0.96, 57.9, 6.04, "#1f2326", rx=2)
def pin(n):
    c = (n - 1) // 2
    return P(8.37 + c * 2.54, 4.77 if n % 2 else 2.23)
USED = {1: C["vcc"], 3: C["sda"], 5: C["scl"], 6: C["gnd"], 4: C["v5"], 8: C["tx"], 9: C["gnd"], 10: C["rx"]}
for n in range(1, 41):
    x, y = pin(n)
    if n in USED:
        circle(x, y, 5.2, USED[n], "#fff", 1.5)
    else:
        rect(x - 2.6, y - 2.6, 5.2, 5.2, GOLD)
x, y = pin(1); text(x - 12, y + 5, "1", 13, "#fff", 700, "end")

# USB-C power lead
ux, uy = P(11.2, 57.2)
wire([(ux, uy), (ux - 34, uy), (ux - 34, 330)], C["usb"], 6, casing=False)
text(36, 300, "USB-C power: official 27 W supply", 14, "#495057", anchor="end", extra='transform="rotate(-90 36 300)"')

# --------------------------------------------------------------- PCA9685 (turned 180 deg)
QX0, QY0 = 720, 420
def Q(u, v):                 # board mm (normal orientation) -> screen, rotated 180
    return QX0 + (62.5 - u) * S, QY0 + (25.4 - v) * S
def qrect(u1, v1, u2, v2, *a, **k):
    ax, ay = Q(u1, v1); bx, by = Q(u2, v2)
    rect2(ax, ay, bx, by, *a, **k)

text(QX0, QY0 - 38, "PCA9685 servo driver", 24, weight=700)
text(QX0, QY0 - 14, "62.5 × 25.4 mm · I²C address 0x40", 14, "#495057")
qrect(0, 0, 62.5, 25.4, "#1f5aa6", "#163f78", 2, rx=2 * S)
for hu, hv in [(3.2, 3.2), (59.3, 3.2), (3.2, 22.2), (59.3, 22.2)]:
    cx, cy = Q(hu, hv); circle(cx, cy, 2.3 * S, "#c9a74a"); circle(cx, cy, 1.25 * S, "#fbf8f3")
# chip + address pads + LED
qrect(26, 9, 36, 15.5, "#1f2326", rx=2)
cx, cy = Q(31, 12.25); text(cx, cy + 5, "PCA9685", 12, "#ced4da", 700, "middle")
for i in range(6):
    qrect(44 + i * 2.2, 2.2, 45.4 + i * 2.2, 3.6, GOLD)
qrect(49.5, 6.2, 51.5, 7.4, "#69db7c")
# control headers at both ends
LABELS = ["GND", "OE", "SCL", "SDA", "VCC", "V+"]
def ctrl(u, i): return Q(u, 6.35 + 2.54 * i)
for u in (1.6, 60.9):
    qrect(u - 1.27, 5.08, u + 1.27, 20.32, "#1f2326")
    for i in range(6):
        x, y = ctrl(u, i)
        rect(x - 2.6, y - 2.6, 5.2, 5.2, GOLD)
for i, lab in enumerate(LABELS):           # silkscreen next to the input header
    x, y = ctrl(60.9, i)
    text(x + 12, y + 4, lab, 11, "#e7f0fb", 700)
# servo outputs: 4 groups of 4, rows PWM / V+ / GND
U0, G = 10.04, 11.6
def ch_u(n): return U0 + (n // 4) * G + (n % 4) * 2.54
ROWS = {"pwm": 18.72, "vp": 21.26, "gnd": 23.8}
for g in range(4):
    qrect(ch_u(4 * g) - 1.27, 17.45, ch_u(4 * g + 3) + 1.27, 25.07, "#1f2326")
for n in range(16):
    for row in ROWS.values():
        x, y = Q(ch_u(n), row)
        rect(x - 2.4, y - 2.4, 4.8, 4.8, GOLD)
    x, y = Q(ch_u(n), 15.6)
    text(x, y + 4, str(n), 10, "#e7f0fb", 700, "middle")
# power terminal (+ / GND), protruding past the edge
qrect(26.5, -1.4, 35.5, 6.5, "#2f9e44", "#1e6b2e", 1.5, rx=3)
T_PLUS, T_GND = Q(28.46, 2.0), Q(33.54, 2.0)
for (x, y) in (T_PLUS, T_GND):
    circle(x, y, 2.0 * S, "#adb5bd", "#495057", 1.5)
    add(f'<line x1="{r(x - 7)}" y1="{r(y)}" x2="{r(x + 7)}" y2="{r(y)}" stroke="#495057" stroke-width="2"/>')
text(T_PLUS[0], T_PLUS[1] - 16, "+", 15, "#fff", 700, "middle")
text(T_GND[0], T_GND[1] - 16, "−", 15, "#fff", 700, "middle")

# --------------------------------------------------------------- 5 V supply
SX, SY = 820, 650
rect(SX, SY, 240, 84, "#495057", rx=12)
text(SX + 120, SY + 36, "5 V 5 A power supply", 19, "#fff", 700, "middle")
text(SX + 120, SY + 60, "for the servos only", 14, "#dee2e6", anchor="middle")
wire([(T_PLUS[0], SY), (T_PLUS[0], QY0 + 25.4 * S + 1.4 * S)], C["v5"], 6, casing=False)
wire([(T_GND[0], SY), (T_GND[0], QY0 + 25.4 * S + 1.4 * S)], C["gnd"], 6, casing=False)

# --------------------------------------------------------------- servos
def servo(x0, y0, name, sub, ch):
    s = S
    rect(x0, y0 + 2 * s, 54.5 * s, 16 * s, "#1c2c55", rx=6)               # flanges
    for hx in (2.6, 51.9):
        for hy in (5.5, 14.5):
            circle(x0 + hx * s, y0 + hy * s, 1.0 * s, "#fbf8f3")
    rect(x0 + 7 * s, y0, 40.5 * s, 20 * s, "#22346a", "#13204a", 2, rx=8)  # body
    cx, cy = x0 + 37.5 * s, y0 + 10 * s
    circle(cx, cy, 6.5 * s, "#2f437f")
    add(f'<rect x="{r(cx - 15 * s)}" y="{r(cy - 2.2 * s)}" width="{r(21 * s)}" height="{r(4.4 * s)}" '
        f'rx="{r(2.2 * s)}" fill="#f8f9fa" stroke="#adb5bd"/>')
    circle(cx, cy, 2.2 * s, "#dee2e6", "#868e96", 1)
    text(x0 + 54.5 * s + 14, y0 + 8 * s, name, 20, weight=700)
    text(x0 + 54.5 * s + 14, y0 + 8 * s + 22, sub, 14, "#495057")
    text(x0 + 54.5 * s + 14, y0 + 8 * s + 42, f"channel {ch}", 14, "#495057")
    return x0 + 7 * s, cy                                                   # lead exit

SVX = 1262
pan = servo(SVX, 170, "Pan", "head left / right", 0)
tilt = servo(SVX, 320, "Tilt", "head up / down", 1)
jaw = servo(SVX, 470, "Jaw", "mouth open / shut", 2)
text(SVX, 640, "Servo leads: brown = ground (outer row) · red = +5 V · orange = signal", 13, "#495057")

def lead(ch, exit_pt, up_y, turn_x):
    x = Q(ch_u(ch), 0)[0]
    ex, ey = exit_pt
    for off, color, row in ((-4.5, "#8b5a2b", "gnd"), (0, "#e03131", "vp"), (4.5, "#f08c00", "pwm")):
        yrow = Q(0, ROWS[row])[1]
        xx = x
        pts = [(xx, yrow), (xx, up_y + off), (turn_x + off, up_y + off), (turn_x + off, ey + off), (ex, ey + off)]
        if abs(ey - up_y) < 1:
            pts = [(xx, yrow), (xx, up_y + off), (ex, ey + off)]
        wire(pts, color, 3, casing=False, rad=6)
lead(0, pan, 352, 1222)
lead(1, tilt, 372, 1202)
lead(2, jaw, 392, 1182)

# --------------------------------------------------------------- radar
RX0, RY0 = 1250, 840
rect(RX0, RY0, 44 * S, 15 * S, "#24324d", "#16203a", 2, rx=8)
for i in range(3):
    for j in range(2):
        rect(RX0 + (18 + i * 7) * S, RY0 + (2.5 + j * 5.5) * S, 4.5 * S, 4.5 * S, "#33456a", rx=3)
rect(RX0 + 1 * S, RY0 + 3.75 * S, 4.5 * S, 7.5 * S, "#f1f3f5", "#adb5bd", 1, rx=2)   # ZH socket
text(RX0 + 44 * S + 14, RY0 + 30, "HLK-LD2450", 22, weight=700)
text(RX0 + 44 * S + 14, RY0 + 52, "mmWave radar · back", 14, "#495057")
text(RX0 + 44 * S + 14, RY0 + 70, "44 × 15 mm · optional", 14, "#495057")
# its 4-wire cable to a 0.1" plug the jumpers go into
DX = 1170
cyc = RY0 + 7.5 * S
plug_y = [cyc + (k - 1.5) * 15.24 for k in range(4)]
sock_y = [RY0 + (5.25 + 1.5 * k) * S for k in range(4)]
for k in range(4):
    wire([(DX + 14, plug_y[k]), (DX + 40, plug_y[k]), (RX0 + 1 * S + 6, sock_y[k])], "#adb5bd", 2.5, casing=False, rad=4)
rect(DX, plug_y[0] - 7.6, 14, 4 * 15.24, "#1f2326", rx=2)
for k, lab in enumerate(["5V", "GND", "TX", "RX"]):
    text(DX + 22, plug_y[k] - 9 if k == 0 else plug_y[k] + 0, "", 1)
text(DX + 14, plug_y[3] + 28, "the radar's own cable", 12, "#495057", anchor="end")
for k, lab in enumerate(["5V", "GND", "TX", "RX"]):
    text(RX0 + 7 * S, sock_y[k] + 3.5, lab, 9, "#e9ecef", 700)

# --------------------------------------------------------------- jumper wires
def jog(n, dy):              # odd pins step half a row so they pass between even pins
    x, y = pin(n)
    return [(x, y), (x + 7, y + dy)]
QV = {lab: ctrl(60.9, i) for i, lab in enumerate(LABELS)}
X_IN = QV["GND"][0]
# Pi -> PCA9685 (lanes: the wire that starts highest turns furthest right)
wire(jog(1, -7.62) + [(560, pin(1)[1] - 7.62), (560, QV["VCC"][1]), (X_IN, QV["VCC"][1])], C["vcc"])
wire(jog(3, -7.62) + [(540, pin(3)[1] - 7.62), (540, QV["SDA"][1]), (X_IN, QV["SDA"][1])], C["sda"])
wire(jog(5, -7.62) + [(520, pin(5)[1] - 7.62), (520, QV["SCL"][1]), (X_IN, QV["SCL"][1])], C["scl"])
wire([pin(6), (500, pin(6)[1]), (500, QV["GND"][1]), (X_IN, QV["GND"][1])], C["gnd"])
# Pi -> radar plug (TX and RX cross over)
wire([pin(4), (488, pin(4)[1]), (488, plug_y[0]), (DX, plug_y[0])], C["v5"])
wire(jog(9, -7.62) + [(472, pin(9)[1] - 7.62), (472, plug_y[1]), (DX, plug_y[1])], C["gnd"])
wire([pin(10), (456, pin(10)[1]), (456, plug_y[2]), (DX, plug_y[2])], C["rx"])
wire([pin(8), (440, pin(8)[1]), (440, plug_y[3]), (DX, plug_y[3])], C["tx"])
# labels on the runs
for lab, y in (("3.3 V → VCC", QV["VCC"][1]), ("SDA", QV["SDA"][1]), ("SCL", QV["SCL"][1]), ("GND", QV["GND"][1])):
    text(704, y - 4, lab, 12, "#495057", anchor="end")
for lab, y in (("5 V (pin 4) → 5V", plug_y[0]), ("GND (pin 9) → GND", plug_y[1]),
               ("radar TX → Pi RX (pin 10)", plug_y[2]), ("Pi TX (pin 8) → radar RX", plug_y[3])):
    text(1150, y - 4, lab, 12, "#495057", anchor="end")
# re-draw the used pin dots on top of the wire ends
for n, col in USED.items():
    x, y = pin(n); circle(x, y, 5.2, col, "#fff", 1.5)

# --------------------------------------------------------------- pin inset
IX, IY = 560, 975
rect(IX, IY, 380, 190, "#fff", "#dee2e6", 1.5, rx=12)
text(IX + 20, IY + 30, "GPIO pins used (enlarged)", 16, weight=700)
text(IX + 20, IY + 50, "Pin 1 is the end farthest from the USB ports", 13, "#6c757d")
NAMES = {1: "3.3 V", 2: "5 V", 3: "SDA", 4: "5 V", 5: "SCL", 6: "GND", 7: "", 8: "TXD", 9: "GND", 10: "RXD"}
for rrow in range(5):
    y = IY + 76 + rrow * 23
    for col, n in ((0, 2 * rrow + 1), (1, 2 * rrow + 2)):
        x = IX + 170 + col * 40
        fill = USED.get(n, "#ced4da")
        circle(x, y, 10, fill, "#fff" if n in USED else "#adb5bd", 1.5)
        text(x, y + 4, str(n), 10, "#fff" if n in USED else "#495057", 700, "middle")
        lab = NAMES[n] + ("" if n in USED else (" (not used)" if NAMES[n] else ""))
        if col == 0:
            text(x - 18, y + 4, lab, 13, "#212529" if n in USED else "#adb5bd", anchor="end")
        else:
            text(x + 18, y + 4, lab, 13, "#212529" if n in USED else "#adb5bd")

# --------------------------------------------------------------- notes
NX, NY = 1250, 975
rect(NX, NY, 500, 230, "#fff4e6", "#f08c00", 2, rx=12)
text(NX + 20, NY + 32, "Before you power up", 18, "#d9480f", 700)
lines = [
    "Servos get power only from the 5 V supply, never from the Pi.",
    "All grounds meet on the PCA9685. Leave its V+ header pin empty.",
    "The radar's TX and RX cross over to the Pi.",
    "install.sh turns on I²C and the serial port for you.",
    "Clone PCA9685 boards: if the servo header's V+ pins read 0 V",
    "with 5 V on the terminal, its protection MOSFET has burned out.",
    "Check the + / − and radar pin labels on your own boards.",
]
for i, l in enumerate(lines):
    text(NX + 20, NY + 62 + i * 22, l, 14, "#343a40")

# --------------------------------------------------------------- USB devices
BY = 1235
def box(x, w, title, sub):
    rect(x, BY, w, 80, "#fff", "#adb5bd", 2, rx=10)
    text(x + w / 2, BY + 32, title, 17, weight=700, anchor="middle")
    text(x + w / 2, BY + 56, sub, 13, "#495057", anchor="middle")
box(40, 300, "USB camera (Arducam 5MP)", "in an eye socket · sees people")
box(360, 270, "USB microphone", "in the collar · hears replies")
box(650, 300, "USB audio adapter", "the Pi 5 has no headphone jack")
box(1010, 230, "Powered speaker", "in the chest · his voice")
u3x, u3y = P(87.2, 26.9)
u2x, u2y = P(87.2, 9.0)
wire([(u3x, u3y), (u3x, 1190), (190, 1190), (190, BY)], C["usb"], 6, casing=False)
wire([(u2x - 14, u2y), (u2x - 14, 1205), (495, 1205), (495, BY)], C["usb"], 6, casing=False)
wire([(u2x + 14, u2y), (u2x + 14, 1176), (800, 1176), (800, BY)], C["usb"], 6, casing=False)
wire([(950, BY + 40), (1010, BY + 40)], "#495057", 5, casing=False)
text(980, BY + 30, "3.5 mm", 12, "#495057", anchor="middle")
text(u3x, u3y + 22, "USB 3", 12, "#495057", anchor="middle")
text(u2x, u2y + 22, "USB 2", 12, "#495057", anchor="middle")
ex, ey = P(87.6, 45.8)
text(ex, ey + 22, "Ethernet", 12, "#adb5bd", anchor="middle")


text(W - 40, H - 16, "Boards drawn to scale (1 mm = 6 px); wires, cables and the boxes aren't. The PCA9685 is drawn turned around so its wires don't cross: both ends' 6-pin headers are the same.", 12, "#868e96", anchor="end")
add("</svg>")
open(sys.argv[1], "w").write("\n".join(out))
