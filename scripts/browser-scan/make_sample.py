"""Generate a small sample floor plan with duplex receptacle symbols."""
import math, pymupdf, sys, base64

W, H = 792, 612  # 11 x 8.5 in
doc = pymupdf.open()
page = doc.new_page(width=W, height=H)
sh = page.new_shape()

def line(a, b, w=1.0):
    sh.draw_line(pymupdf.Point(*a), pymupdf.Point(*b)); sh.finish(color=(0,0,0), width=w)

def rect(x0, y0, x1, y1, w=1.0):
    sh.draw_rect(pymupdf.Rect(x0, y0, x1, y1)); sh.finish(color=(0,0,0), width=w)

# Sheet border and title block.
rect(18, 18, W-18, H-18, 1.5)
rect(W-238, H-78, W-18, H-18, 1.0)
# Walls (double lines).
def wall(x0, y0, x1, y1):
    if y0 == y1:
        line((x0, y0-3), (x1, y1-3), 1.4); line((x0, y0+3), (x1, y1+3), 1.4)
    else:
        line((x0-3, y0), (x1-3, y1), 1.4); line((x0+3, y0), (x1+3, y1), 1.4)
wall(60, 60, 560, 60); wall(60, 460, 560, 460); wall(60, 60, 60, 460); wall(560, 60, 560, 460)
wall(300, 60, 300, 300); wall(60, 300, 420, 300); wall(420, 300, 420, 460)
# Door swings.
for cx, cy, a0 in [(300, 330, 0), (180, 300, 90), (420, 380, 180)]:
    sh.draw_sector(pymupdf.Point(cx, cy), pymupdf.Point(cx+30*math.cos(math.radians(a0)), cy+30*math.sin(math.radians(a0))), 90, fullSector=True)
    sh.finish(color=(0,0,0), width=0.6)
sh.commit()

def receptacle(cx, cy, facing):
    """Duplex receptacle: circle, two parallel slots, leader toward the wall.
    facing = direction of the wall from the symbol: 'up','down','left','right'."""
    s = page.new_shape()
    s.draw_circle(pymupdf.Point(cx, cy), 5.5)
    if facing in ("up", "down"):
        s.draw_line(pymupdf.Point(cx-2.2, cy-3.2), pymupdf.Point(cx-2.2, cy+3.2))
        s.draw_line(pymupdf.Point(cx+2.2, cy-3.2), pymupdf.Point(cx+2.2, cy+3.2))
        d = -1 if facing == "up" else 1
        s.draw_line(pymupdf.Point(cx, cy+d*5.5), pymupdf.Point(cx, cy+d*10))
    else:
        s.draw_line(pymupdf.Point(cx-3.2, cy-2.2), pymupdf.Point(cx+3.2, cy-2.2))
        s.draw_line(pymupdf.Point(cx-3.2, cy+2.2), pymupdf.Point(cx+3.2, cy+2.2))
        d = -1 if facing == "left" else 1
        s.draw_line(pymupdf.Point(cx+d*5.5, cy), pymupdf.Point(cx+d*10, cy))
    s.finish(color=(0,0,0), width=0.9)
    s.commit()

recs = [
    (120.0, 73.0, "up"), (220.0, 73.0, "up"), (400.0, 73.0, "up"), (500.0, 73.0, "up"),
    (73.0, 150.0, "left"), (73.0, 240.0, "left"), (547.0, 180.0, "right"), (547.0, 380.0, "right"),
    (287.0, 200.0, "right"), (140.0, 287.0, "down"), (250.0, 287.0, "down"),
    (100.0, 447.0, "down"), (240.0, 447.0, "down"), (480.0, 447.0, "down"), (433.0, 400.0, "left"),
]
for r in recs:
    receptacle(*r)

# Switches, labels, notes.
for x, y in [(330, 318), (200, 318), (440, 400), (80, 80)]:
    page.insert_text((x, y), "S", fontsize=9, fontname="helv")
for x, y, t in [(150, 180, "OFFICE 101"), (400, 180, "OFFICE 102"), (220, 380, "OPEN WORK 103"), (470, 380, "STORAGE")]:
    page.insert_text((x, y), t, fontsize=8, fontname="helv")
page.insert_text((W-230, H-58), "SAMPLE FLOOR PLAN - POWER", fontsize=9, fontname="helv")
page.insert_text((W-230, H-44), "E-101   SCALE 1/8\" = 1'-0\"", fontsize=8, fontname="helv")
page.insert_text((W-230, H-30), "Generated test sheet, not a real project", fontsize=7, fontname="helv")
# Legend with one symbol.
page.insert_text((600, 90), "LEGEND", fontsize=8, fontname="helv")
receptacle(612, 110, "up"); page.insert_text((630, 113), "DUPLEX RECEPTACLE", fontsize=7, fontname="helv")

out = sys.argv[1]
doc.save(out, garbage=4, deflate=True)
print(len(open(out, "rb").read()), "bytes;", len(recs) + 1, "receptacles incl. legend")
for cx, cy, f in recs:
    print(f"  {cx*200/72:.1f},{cy*200/72:.1f} {f}")
