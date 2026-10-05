"""Builds Pinny-User-Guide.pdf from the screenshots in shots/.

    node docs/guide/shoot.mjs docs/guide/shots   # refresh the screenshots
    python docs/guide/build.py                    # needs reportlab and pillow

Cropped copies (*-crop.png) are written next to the screenshots; they are
not committed."""
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

HERE = Path(__file__).parent
SHOTS = HERE / "shots"
OUT = HERE / "Pinny-User-Guide.pdf"

F = "/usr/share/fonts/truetype/dejavu/"
pdfmetrics.registerFont(TTFont("Sans", F + "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Bold", F + "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Mono", F + "DejaVuSansMono.ttf"))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans", boldItalic="Sans-Bold")

INK = colors.HexColor("#1d2330")
MUTED = colors.HexColor("#5b6472")
ACCENT = colors.HexColor("#1f5fbf")
TIP_BG = colors.HexColor("#eef4fd")
WARN_BG = colors.HexColor("#fff6e0")
RULE = colors.HexColor("#d6dbe3")

base = ParagraphStyle("base", fontName="Sans", fontSize=10, leading=14.5, textColor=INK, alignment=TA_LEFT)
S = {
    "body": base,
    "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=12, textColor=MUTED),
    "caption": ParagraphStyle("caption", parent=base, fontSize=8.5, leading=11, textColor=MUTED, spaceBefore=3,
                              spaceAfter=10),
    "h1": ParagraphStyle("h1", parent=base, fontName="Sans-Bold", fontSize=20, leading=25, spaceBefore=4,
                         spaceAfter=10, textColor=ACCENT),
    "h2": ParagraphStyle("h2", parent=base, fontName="Sans-Bold", fontSize=13.5, leading=18, spaceBefore=14,
                         spaceAfter=6),
    "h3": ParagraphStyle("h3", parent=base, fontName="Sans-Bold", fontSize=11, leading=15, spaceBefore=8,
                         spaceAfter=3),
    "code": ParagraphStyle("code", parent=base, fontName="Mono", fontSize=8.6, leading=12, backColor=colors.HexColor("#f3f4f6"),
                           borderPadding=(5, 6, 5, 6), spaceBefore=4, spaceAfter=8, leftIndent=6, rightIndent=6),
    "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=12),
    "cellb": ParagraphStyle("cellb", parent=base, fontName="Sans-Bold", fontSize=9, leading=12),
}
W = letter[0] - 2 * 0.85 * inch


class Doc(SimpleDocTemplate):
    def afterFlowable(self, f):
        if isinstance(f, Paragraph) and f.style.name in ("h1", "h2"):
            level = 0 if f.style.name == "h1" else 1
            text = f.getPlainText()
            key = f"k{id(f)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))


def footer(canv, doc):
    canv.saveState()
    canv.setFont("Sans", 8)
    canv.setFillColor(MUTED)
    canv.drawString(0.85 * inch, 0.55 * inch, "Pinny user guide - reviewing drawings and training the models")
    canv.drawRightString(letter[0] - 0.85 * inch, 0.55 * inch, f"Page {doc.page}")
    canv.restoreState()


def cover(canv, doc):
    canv.saveState()
    canv.setFillColor(ACCENT)
    canv.rect(0, letter[1] - 3.6 * inch, letter[0], 3.6 * inch, stroke=0, fill=1)
    canv.setFillColor(colors.white)
    canv.setFont("Sans-Bold", 40)
    canv.drawString(0.85 * inch, letter[1] - 1.9 * inch, "Pinny")
    canv.setFont("Sans", 17)
    canv.drawString(0.85 * inch, letter[1] - 2.5 * inch, "User guide: reviewing drawings and training the models")
    canv.setFont("Sans", 10.5)
    canv.drawString(0.85 * inch, letter[1] - 3.0 * inch, "For reviewers and admins  |  October 2026  |  Pinny training-site 77c9b5e")
    canv.restoreState()


def P(text, style="body"):
    return Paragraph(text, S[style])


def steps(items, start=1):
    return ListFlowable([ListItem(P(t), leftIndent=16, value=i) for i, t in enumerate(items, start)],
                        bulletType="1", start=start, leftIndent=16, bulletFontName="Sans-Bold", bulletFontSize=10)


def bullets(items):
    return ListFlowable([ListItem(P(t), leftIndent=12) for t in items], bulletType="bullet", start="•",
                        leftIndent=12, bulletFontName="Sans")


def box(text, kind="tip"):
    label = {"tip": "Tip", "warn": "Important", "note": "Note"}[kind]
    bg = WARN_BG if kind == "warn" else TIP_BG
    t = Table([[P(f"<b>{label}.</b> {text}")]], colWidths=[W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LEFTPADDING", (0, 0), (-1, -1), 9),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return KeepTogether([Spacer(1, 4), t, Spacer(1, 6)])


def table(rows, widths, header=True):
    data = [[P(c, "cellb" if header and r == 0 else "cell") for c in row] for r, row in enumerate(rows)]
    t = Table(data, colWidths=[w * W for w in widths], repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef0f4")))
    t.setStyle(TableStyle(st))
    return KeepTogether([t, Spacer(1, 8)])


def shot(name, caption, width=1.0, crop=None):
    path = SHOTS / f"{name}.png"
    im = PILImage.open(path)
    if crop:  # fractions (left, top, right, bottom)
        w, h = im.size
        im = im.crop((int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h)))
        path = SHOTS / f"{name}-crop.png"  # build output, git-ignored
        im.save(path)
    w, h = im.size
    dw = W * width
    dh = dw * h / w
    if dh > 4.6 * inch:
        dh = 4.6 * inch
        dw = dh * w / h
    img = Image(str(path), width=dw, height=dh)
    frame = Table([[img]], colWidths=[dw + 2])
    frame.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 1),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return KeepTogether([Spacer(1, 4), frame, P(caption, "caption")])


story = [Spacer(1, 3.4 * inch)]
story += [
    P("This guide has two parts. <b>Part 1</b> is for everyone who reviews drawings: signing in, scanning, and "
      "marking each pin Correct, Wrong or Missed. <b>Part 2</b> is for admins: members, the dashboard, building "
      "datasets, training the two models, and promoting them."),
    Spacer(1, 10),
    P("<b>Site address:</b> https://pinny.ecinc.us (production) and https://staging.pinny.ecinc.us (staging, for "
      "trying changes first)."),
    P("<b>Hours:</b> the server runs 7 AM to 7 PM Eastern every day and is switched off at night."),
    Spacer(1, 14),
    P("Screenshots come from the current version of Pinny running a small test drawing, so your drawings and counts "
      "will look different.", "small"),
    PageBreak(),
    P("Contents", "h1"),
]
toc = TableOfContents()
toc.levelStyles = [ParagraphStyle("t0", parent=base, fontName="Sans-Bold", fontSize=10.5, leading=16, leftIndent=0),
                   ParagraphStyle("t1", parent=base, fontSize=9.5, leading=13.5, leftIndent=16)]
story += [toc, PageBreak()]

# ------------------------------------------------------------------ Part 1
story += [
    P("Part 1 - Reviewing drawings", "h1"),
    P("1. What Pinny does", "h2"),
    P("Pinny finds electrical devices (receptacles and other legend symbols) on drawing PDFs and puts a <b>pin</b> on "
      "each one it finds. You check every pin. Each check does two jobs: it makes that drawing's count right, and "
      "it becomes a training example that teaches Pinny's models to find devices better."),
    P("Pinny finds devices three ways today, with no training needed:"),
    bullets([
        "<b>Legend and whole-set scan</b> - Pinny reads the drawing set's symbol legend, then looks for every legend "
        "symbol on every sheet and counts them by type. Best for complete drawing sets.",
        "<b>Template matching</b> - you draw a box around one example symbol and Pinny finds look-alikes on the "
        "page, at any of the four rotations, mirrored, and somewhat smaller or larger.",
        "<b>Vector matching</b> - for PDFs exported from CAD, Pinny compares the actual line drawing rather than a "
        "picture, which is more exact.",
    ]),
    P("Two learned models come later, once your team has reviewed enough pins (Part 2): the <b>verifier</b>, which "
      "double-checks every template match and throws out false ones, and the <b>point detector</b>, which finds "
      "devices on its own with no template."),

    P("2. Signing in", "h2"),
    steps([
        "Your admin adds you and sends you a <b>one-time set-password link</b> (valid for 72 hours).",
        "Open the link, choose a password of at least 12 characters (a password manager helps) and type it twice.",
        "You are signed in. From then on, go to the site address and sign in with your email and password.",
    ]),
    shot("01-set-password", "The set-password page that the one-time link opens.", 0.45, crop=(0.34, 0.18, 0.66, 0.88)),
    box("Forgot your password? Ask an admin for a new link (Reset password in the Members panel). "
        "A wrong password too many times blocks sign-in for a few minutes."),
    P("You stay signed in for up to a week of inactivity (30 days at most). Use <b>Sign out</b> at the top of the "
      "panel on a shared computer."),

    P("3. Open a drawing", "h2"),
    steps([
        "In the <b>Viewer</b>, under <b>1. Document</b>, press <b>Choose File</b> and pick a PDF (up to 200 MB), "
        "or choose a drawing someone already uploaded from the <b>open</b> list.",
        "Wait for \"<i>name</i>.pdf: N page(s).\" The pages appear under <b>2. Page</b>.",
    ]),
    P("Everyone on the team sees every uploaded drawing. Only the person who uploaded a drawing, or an admin, can "
      "press <b>Delete this drawing</b>."),

    P("4. Scan for devices", "h2"),
    P("Pick one of the two ways below. For a full set with a symbol legend, use A. For a single sheet, or when the "
      "legend can't be read, use B."),
    P("A. Whole set, using the legend (recommended for drawing sets)", "h3"),
    steps([
        "Under <b>Legend: count the whole set</b>, press <b>Find legend</b>. Pinny finds the legend page and reads "
        "its rows (symbol, tag, description). If it picked the wrong page, open the legend page and press "
        "<b>Read legend on this page</b>.",
        "Open <b>Legend symbols</b> and check each row. Click a row to fix its name or tag, press <b>Looks right</b>, "
        "<b>Split</b> a row that holds two symbols, or <b>Add a missing symbol</b> by drawing a box around it.",
        "Press <b>Confirm legend</b>.",
        "Under <b>Whole set</b>, press <b>Scan whole set</b>. Pinny scans every sheet and lists each one with how "
        "many symbols it found and how many are left to review.",
        "Press <b>Next sheet to review</b> and review that sheet's pins (sections 5-7). Repeat until every sheet "
        "is marked reviewed.",
    ]),
    shot("14-legend", "A confirmed legend with five symbols, and the whole-set results: two sheets ready to mark "
         "reviewed, one still to review.", 1.0, crop=(0, 0.12, 0.66, 1)),
    P("B. One symbol, using a template box", "h3"),
    steps([
        "Pick the page under <b>2. Page</b> and zoom in (mouse wheel, + / -, or pinch on a tablet) until one "
        "receptacle is clear.",
        "Choose <b>Template box (T)</b> in the toolbar and drag a snug rectangle around that one symbol.",
        "Leave <b>Threshold</b> at 0.65 to start. Lower finds more but adds false hits; higher finds fewer.",
        "Press <b>Scan this page</b>, or type pages in <b>Pages to scan</b> (blank = all) and press <b>Scan all "
        "pages</b> to scan the set with the same template. Batch progress shows under <b>Batch</b>; press "
        "<b>Next mark to review</b> to step through every page's pins.",
    ]),
    shot("04-template-box", "Template box mode: a box drawn around one receptacle (\"Template: x 278, y 278, "
         "44 x 44 px\" in the panel).", 1.0, crop=(0, 0, 0.72, 0.76)),
    box("Scanning again creates a new scan with fresh, unreviewed pins. The earlier scan and its reviews are kept "
        "and can be reopened from the Scan list.", "note"),

    P("5. Review each pin: Correct or Wrong", "h2"),
    P("Pins are drawn on the page in colour:"),
    table([["Colour", "Meaning"],
           ["Orange", "Not reviewed yet"],
           ["Green", "Approved (Correct)"],
           ["Magenta", "Added by hand (a missed device)"],
           ["Grey x", "Rejected (Wrong) - hidden unless \"Show rejected and removed pins\" is ticked"],
           ["Dashed ring / red ring", "Still saving / did not save (see section 9)"]], [0.28, 0.72]),
    steps([
        "Click a pin (in <b>Pan / pick pin (V)</b> mode). A small popup appears beside it.",
        "Press <b>Correct</b> (key <b>A</b>) if it really is that device, or <b>Wrong</b> (key <b>X</b>) if not.",
        "Press <b>Next</b> (key <b>N</b>) to jump to the next unreviewed pin. The view follows it.",
    ]),
    shot("05b-pin-popup-close", "The popup beside a selected pin.", 0.7),
    P("Made a mistake? The pin stays selected after you press a button, so you can press the other one right away. "
      "For a pin you added by hand the popup offers <b>Delete</b> instead of Wrong."),
    box("Be strict and consistent. A pin is Correct only if it sits on a real device of the type being counted. "
        "Text, tags, look-alike symbols and crossed-out devices are Wrong. These clicks are exactly what the models "
        "learn from."),

    P("6. Mark missed devices", "h2"),
    P("A device the scan did not find has no pin, so marking it has its own button on the drawing."),
    steps([
        "Press <b>+ Missed</b> (bottom-left of the drawing), or the key <b>M</b>.",
        "Click (or tap) the centre of the device the scan missed. A magenta pin appears and Pinny goes back to "
        "reviewing with the new pin selected.",
        "Repeat for each missed device. To cancel, press + Missed again or <b>Esc</b>.",
    ]),
    shot("06-missed", "The + Missed button while marking: tap the missed device, or press Esc to cancel.", 1.0,
         crop=(0.2, 0.45, 0.85, 1.0)),
    P("Marking many in a row? Choose <b>Mark missed (P)</b> in the toolbar: it stays on for every click until you "
      "switch back to Pan (V)."),

    P("7. Mark the page fully reviewed", "h2"),
    P("When every device on a page has a correct pin and no orange (unreviewed) pins are left, press <b>Mark page "
      "fully reviewed</b> in the side panel (or on the Label queue)."),
    box("Only fully reviewed pages can train the point detector, so this step matters. Mark a page only when you "
        "have also added every missed device. If someone rescans the page later, it reopens for review.", "warn"),

    P("8. The Label queue", "h2"),
    P("<b>Label queue</b> (top of the panel, or on the training pages) lists pages with the <b>most uncertain "
      "pins first</b>: the ones whose scores sit closest to the threshold. Reviewing these teaches the models the "
      "most per click."),
    steps([
        "Open <b>Label queue</b>.",
        "Press <b>Review</b> on a page. The viewer opens on its most uncertain pin.",
        "Review the page, then come back and press <b>Mark fully reviewed</b> when its status says "
        "\"ready to mark\".",
    ]),
    shot("08-label-queue", "The Label queue: pages needing review, and the most uncertain pins.", 1.0,
         crop=(0, 0, 1, 0.55)),

    P("9. Saving, shortcuts and tips", "h2"),
    P("Every click is saved automatically. The panel shows \"Saving N edits...\", \"All changes saved\" or "
      "\"N edits not saved\". Edits survive a page reload. If something doesn't save, press <b>Retry</b> (or "
      "<b>Discard</b>) next to it. If your sign-in expires, Pinny asks you to sign in again and then sends the "
      "waiting edits."),
    table([["Key", "Action", "Key", "Action"],
           ["A", "Correct (approve)", "V", "Pan / pick pin"],
           ["X or Delete", "Wrong (reject) / delete a hand-added pin", "T", "Template box"],
           ["M", "Mark one missed device", "P", "Mark missed (stays on)"],
           ["N", "Next unreviewed pin", "B", "Next mark in a batch"],
           ["+ / - / 0", "Zoom in / out / fit", "R", "Rotate the view 90 degrees"],
           ["H", "Hide pins", "Esc", "Cancel / deselect"]], [0.13, 0.37, 0.13, 0.37]),
    bullets([
        "Hold the space bar and drag, or drag with the middle mouse button, to pan in any mode.",
        "On a tablet: drag with one finger to pan, pinch to zoom, double-tap to zoom in.",
        "Finish your session before 7 PM Eastern: the server switches off then.",
    ]),
    PageBreak(),
]

# ------------------------------------------------------------------ Part 2
story += [
    P("Part 2 - Admins: training the models", "h1"),
    P("Admins see extra pages along the top of the training site: <b>Datasets</b>, <b>Training runs</b> and "
      "<b>Team</b>, plus the <b>Members</b> and <b>OCR</b> panels at the bottom of the viewer."),

    P("10. Add and manage members", "h2"),
    steps([
        "In the viewer, scroll to <b>Members (admin)</b>.",
        "Type the person's email, choose <b>Reviewer</b> or <b>Admin</b>, and press <b>Add or change</b>.",
        "Copy the one-time set-password link that appears and send it to them privately (text or in person). "
        "It is valid for 72 hours.",
        "For a forgotten password, press <b>Reset password</b> on their row and send the new link. To take someone "
        "off Pinny, press <b>Remove</b>. Both end that person's sessions at once.",
    ]),
    P("Reviewers can upload, scan and review. Admins also manage members, delete anyone's uploads, and train and "
      "promote models. Keep admins to one or two people."),

    P("11. Watch progress: Dashboard and Team", "h2"),
    P("The <b>Dashboard</b> counts labels and pages and shows a <b>Readiness</b> card for each model."),
    shot("07-dashboard", "Dashboard with the readiness cards. Red numbers are below target.", 1.0,
         crop=(0, 0, 0.83, 0.72)),
    table([["Model", "Ready to train when you have", "Typical time with 2-3 reviewers"],
           ["Verifier", "100 Correct and 100 Wrong pins, from 10 drawing sets", "A few days"],
           ["Point detector", "30 pages marked fully reviewed, 300 pins on them, from 10 drawing sets",
            "About two weeks"],
           ["Promoting any model", "A real test set of at least 3 drawing sets and 100 pins, and beating the "
            "template scanner", "-"]], [0.2, 0.52, 0.28]),
    P("These are guidance: an admin can train earlier, but a model trained on too little data won't pass the "
      "promotion gate. <b>Team</b> shows who uploaded, scanned and reviewed what, with a per-day table; click a "
      "person to see their recent actions."),
    shot("13-team", "Team progress: per-person totals and review actions per day.", 1.0, crop=(0, 0, 0.6, 0.55)),

    P("12. Train each legend symbol (Symbol types)", "h2"),
    P("This is the quickest win and needs no special amounts of data: each legend symbol learns from its own "
      "Correct / Wrong reviews from whole-set scans."),
    steps([
        "Open <b>Symbol types</b>. Each tag shows how many of its pins were approved, rejected and added by hand.",
        "Press <b>Train</b> (or <b>Train every symbol with reviews</b>). It takes seconds to a few minutes.",
        "Pinny checks the result on reviews it didn't train on. Learning switches on only if it keeps every correct "
        "match. Use <b>Switch off</b> / <b>Switch on</b> to control it.",
        "The next <b>Scan whole set</b> throws out matches that look like that symbol's rejected examples.",
    ]),
    shot("09-symbol-types", "Symbol types after training: learning on for symbols that passed the check.", 1.0,
         crop=(0, 0, 1, 0.5)),

    P("13. Build a dataset", "h2"),
    steps([
        "Open <b>Datasets</b> and press <b>Build dataset from current reviews</b>.",
        "Wait for it on <b>Training runs</b>. The dataset is a frozen snapshot of all reviews, split by drawing set "
        "into train, validation and test, so a model is always tested on drawings it never saw.",
        "Check the <b>Test split</b> column: it must say \"enough to promote\" (3 documents, 100 points) before any "
        "model trained on it can be promoted.",
    ]),
    shot("10-datasets", "Datasets: one row per snapshot, with its train/val/test counts.", 1.0, crop=(0, 0, 1, 0.45)),
    box("Build a new dataset whenever a good batch of new reviews has come in. Old datasets stay, so you can always "
        "compare.", "tip"),

    P("14. Train the verifier and the point detector", "h2"),
    steps([
        "On <b>Datasets</b>, set <b>Epochs</b> (30 is a good default) and press <b>Train verifier</b> or "
        "<b>Train detector</b> on the dataset's row.",
        "Follow it on <b>Training runs</b>: progress bar, status and a live log. <b>Cancel</b> stops it.",
        "When it finishes, the new model appears on <b>Models</b> as a <b>candidate</b>. It is not used for scanning "
        "until you promote it.",
    ]),
    shot("11-training-runs", "Training runs: a running job with its progress and log.", 1.0, crop=(0, 0, 1, 0.75)),
    box("Training runs on the server's processor and can take hours for the detector. Start long runs in the "
        "morning: the server stops at 7 PM Eastern, and an interrupted run must be started again by hand.", "warn"),

    P("15. Benchmark and promote", "h2"),
    steps([
        "On <b>Models</b>, press <b>Benchmark</b> on the candidate. It runs the candidate and the plain template "
        "scanner on the dataset's test drawings.",
        "Read the side-by-side table: <b>Precision</b> (how many of its pins were right), <b>Recall</b> (how many "
        "real devices it found), and the pass/fail checks under it.",
        "If the gate says <b>recommends promotion</b>, press <b>Promote</b>. Promote stays greyed out otherwise.",
        "To undo, press <b>Deactivate</b> on the active model: scans go back to template matching only.",
    ]),
    shot("12-models", "Models: one candidate fails the gate (fewer devices found than the template scanner), the "
         "other passes and can be promoted.", 1.0, crop=(0, 0, 1, 0.97)),

    P("16. Scan with the trained models", "h2"),
    P("Once promoted, the models appear in the viewer's <b>Scan mode</b> list under <b>3. Scan with a template</b>:"),
    table([["Scan mode", "Needs", "Use it when"],
           ["Template match", "A template box", "Always available; the baseline"],
           ["Template + verifier", "A template box and an active verifier", "Template matching gives too many false "
            "hits; the verifier removes them"],
           ["Point detector", "An active detector, no template", "You want devices found without drawing a box"]],
          [0.24, 0.33, 0.43]),
    P("Keep reviewing in every mode: new Correct / Wrong / Missed clicks keep improving the next model you train. "
      "A good rhythm is: review for a week, build a dataset, train, benchmark, and promote only when the gate "
      "agrees."),

    KeepTogether([P("17. Optional: read sheet info with OCR", "h2"),
    P("In the viewer's <b>OCR (admin)</b> panel, choose <b>Tesseract</b> and press <b>Save</b>. Everyone then gets "
      "<b>Read sheet info</b> under each page, which reads the sheet number, title and revision from the title "
      "block. OCR does not find devices; it only labels sheets. (The server needs Tesseract installed once.)")]),
]

doc = Doc(str(OUT), pagesize=letter, leftMargin=0.85 * inch, rightMargin=0.85 * inch, topMargin=0.8 * inch,
          bottomMargin=0.85 * inch, title="Pinny user guide", author="Pinny",
          subject="Reviewing drawings and training the models")
doc.multiBuild(story, onFirstPage=cover, onLaterPages=footer)
print(OUT)
