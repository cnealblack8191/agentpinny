-- Store schema 2 database written by pinny.learning at commit fcb2728
-- (scan-1 with det-1 approved by alice, det-2 rejected by bob, one manual pin).
BEGIN TRANSACTION;
CREATE TABLE crops (
    crop_key    TEXT PRIMARY KEY,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending','written','failed')),
    rel_path    TEXT,
    sha256      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
INSERT INTO "crops" VALUES('40927726de3f790ef41d8081c5047939f24d6008dc58836feed13dd09d7e3ba8','{"box":{"height":88,"width":88,"x":76,"y":76},"canonical_page_id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa#p0","clipped":false,"clipped_bottom":false,"clipped_left":false,"clipped_right":false,"clipped_top":false,"document_version":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","dpi":200,"frame_height":2200,"frame_width":1700,"method":"detection_box_margin","page_index":0,"spec_version":2,"unclipped_box":{"height":88,"width":88,"x":76,"y":76}}','written','crops/40/40927726de3f790ef41d8081c5047939f24d6008dc58836feed13dd09d7e3ba8.png','53f8df4dc8c40e44bce7d88c88c4018831080cb6e04da17116d054df4f70b9b6',1,NULL,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
INSERT INTO "crops" VALUES('257f939b002ffb38793458f140b96ebe432da8ac4a10c5b1e133e5aa05dc57c8','{"box":{"height":88,"width":88,"x":276,"y":276},"canonical_page_id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa#p0","clipped":false,"clipped_bottom":false,"clipped_left":false,"clipped_right":false,"clipped_top":false,"document_version":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","dpi":200,"frame_height":2200,"frame_width":1700,"method":"detection_box_margin","page_index":0,"spec_version":2,"unclipped_box":{"height":88,"width":88,"x":276,"y":276}}','written','crops/25/257f939b002ffb38793458f140b96ebe432da8ac4a10c5b1e133e5aa05dc57c8.png','afe04379e8afc8c09b727d58f5075cdd6d82743544596da6e7e20bbabceb0d47',1,NULL,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
INSERT INTO "crops" VALUES('b6f87ee420fbbbd6d39f91e8808fd61c6c6dc21c7310e0bc3703053d0910d58e','{"box":{"height":124,"width":115,"x":0,"y":0},"canonical_page_id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa#p0","clipped":true,"clipped_bottom":false,"clipped_left":true,"clipped_right":false,"clipped_top":true,"document_version":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","dpi":200,"frame_height":2200,"frame_width":1700,"method":"manual_point_square","page_index":0,"spec_version":2,"unclipped_box":{"height":128,"width":128,"x":-13,"y":-4}}','written','crops/b6/b6f87ee420fbbbd6d39f91e8808fd61c6c6dc21c7310e0bc3703053d0910d58e.png','ff8e00f7e5825b480ee726921fded982c984251b71a7f9a8d75c3192e132d97c',1,NULL,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
CREATE TABLE detections (
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    detection_id TEXT NOT NULL,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    x  REAL NOT NULL, y  REAL NOT NULL,
    score        REAL NOT NULL,
    rotation     INTEGER NOT NULL,
    source       TEXT NOT NULL,
    raw          TEXT NOT NULL,
    PRIMARY KEY (scan_id, detection_id)
);
INSERT INTO "detections" VALUES('scan-1','det-1',100.0,100.0,140.0,140.0,120.0,120.0,0.9,0,'detector','{}');
INSERT INTO "detections" VALUES('scan-1','det-2',300.0,300.0,340.0,340.0,320.0,320.0,0.7,90,'detector','{}');
INSERT INTO "detections" VALUES('scan-1','det-3',500.0,500.0,540.0,540.0,520.0,520.0,0.6,0,'detector','{}');
CREATE TABLE pins (
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    pin_id       TEXT NOT NULL,
    origin       TEXT NOT NULL CHECK (origin IN ('machine','manual')),
    state        TEXT NOT NULL CHECK (state IN ('unreviewed','approved','rejected','added','removed')),
    detection_id TEXT,
    x REAL NOT NULL, y REAL NOT NULL,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    version      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (scan_id, pin_id),
    FOREIGN KEY (scan_id, detection_id) REFERENCES detections(scan_id, detection_id)
);
INSERT INTO "pins" VALUES('scan-1','det-1','machine','approved','det-1',120.0,120.0,100.0,100.0,140.0,140.0,2,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
INSERT INTO "pins" VALUES('scan-1','det-2','machine','rejected','det-2',320.0,320.0,300.0,300.0,340.0,340.0,2,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
INSERT INTO "pins" VALUES('scan-1','det-3','machine','unreviewed','det-3',520.0,520.0,500.0,500.0,540.0,540.0,1,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
INSERT INTO "pins" VALUES('scan-1','69f48215-3d47-58c5-ad1c-3b5f71cf60f6','manual','added',NULL,50.5,60.25,NULL,NULL,NULL,NULL,1,'2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
CREATE TABLE review_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    request_id   TEXT NOT NULL UNIQUE,
    request_sha  TEXT NOT NULL,
    scan_id      TEXT NOT NULL REFERENCES scans(scan_id),
    pin_id       TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('approve','reject','add_manual','remove_manual')),
    prior_state  TEXT,
    new_state    TEXT NOT NULL,
    source       TEXT NOT NULL,
    reviewer     TEXT,
    crop_key     TEXT REFERENCES crops(crop_key),
    created_at   TEXT NOT NULL,
    FOREIGN KEY (scan_id, pin_id) REFERENCES pins(scan_id, pin_id)
);
INSERT INTO "review_events" VALUES(1,'4a5deacc-6467-5655-8205-d00cc222efcf','legacy-r1','9bbc9dc8357fc20e834a8123cec6a5c6825871259b9930ee828b7d1bd3f0afad','scan-1','det-1','approve','{"box":{"height":40,"width":40,"x":100,"y":100},"detection_id":"det-1","origin":"machine","pin_id":"det-1","point":{"x":120.0,"y":120.0},"state":"unreviewed","version":1}','{"box":{"height":40,"width":40,"x":100,"y":100},"detection_id":"det-1","origin":"machine","pin_id":"det-1","point":{"x":120.0,"y":120.0},"state":"approved","version":2}','viewer','alice','40927726de3f790ef41d8081c5047939f24d6008dc58836feed13dd09d7e3ba8','2026-09-23T12:00:00.000Z');
INSERT INTO "review_events" VALUES(2,'c748b3e5-98b7-52ab-82c0-ffa95c3ee68e','legacy-r2','2a47a1085c5d583e38e01f76101eb308f6a3c30d28ec34e182c7e54b9b50445e','scan-1','det-2','reject','{"box":{"height":40,"width":40,"x":300,"y":300},"detection_id":"det-2","origin":"machine","pin_id":"det-2","point":{"x":320.0,"y":320.0},"state":"unreviewed","version":1}','{"box":{"height":40,"width":40,"x":300,"y":300},"detection_id":"det-2","origin":"machine","pin_id":"det-2","point":{"x":320.0,"y":320.0},"state":"rejected","version":2}','viewer','bob','257f939b002ffb38793458f140b96ebe432da8ac4a10c5b1e133e5aa05dc57c8','2026-09-23T12:00:00.000Z');
INSERT INTO "review_events" VALUES(3,'ac50679b-17c2-5f43-b17a-64195e421944','legacy-r3','4d66603ecd8ecadc248e2ba3bbd3234a31f25103d36d4fb0948a1f33f5c81e18','scan-1','69f48215-3d47-58c5-ad1c-3b5f71cf60f6','add_manual',NULL,'{"box":null,"detection_id":null,"origin":"manual","pin_id":"69f48215-3d47-58c5-ad1c-3b5f71cf60f6","point":{"x":50.5,"y":60.25},"state":"added","version":1}','viewer','alice','b6f87ee420fbbbd6d39f91e8808fd61c6c6dc21c7310e0bc3703053d0910d58e','2026-09-23T12:00:00.000Z');
CREATE TABLE scans (
    scan_id               TEXT PRIMARY KEY,
    document_id           TEXT NOT NULL,
    document_version      TEXT NOT NULL,
    page_index            INTEGER NOT NULL,
    canonical_page_id     TEXT NOT NULL,
    frame_width           INTEGER NOT NULL,
    frame_height          INTEGER NOT NULL,
    dpi                   INTEGER NOT NULL,
    template_box          TEXT,
    template_sha256       TEXT,
    detector_name         TEXT NOT NULL,
    detector_version      TEXT NOT NULL,
    detector_settings     TEXT NOT NULL,
    detector_settings_sha TEXT NOT NULL,
    metadata              TEXT NOT NULL,
    fingerprint           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    recorded_at           TEXT NOT NULL
);
INSERT INTO "scans" VALUES('scan-1','doc-1','sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',0,'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa#p0',1700,2200,200,'{"height":40,"width":40,"x":10,"y":10}','ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff','opencv-template','git:abc123','{"rotations":[0,90],"threshold":0.8}','ac0f395033e2dc4ad4a5beb91e6dfbaca30d5d796a225b6bfc390293065de5f4','{}','a224e86aa8448c5a703c8e15d3ce994b8bdcc0a2c43cadb325072941c9cc3e56','2026-09-23T12:00:00.000Z','2026-09-23T12:00:00.000Z');
CREATE TABLE store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO "store_meta" VALUES('schema_version','2');
CREATE INDEX scans_by_page ON scans(document_version, page_index);
CREATE INDEX events_by_pin ON review_events(scan_id, pin_id, seq);
CREATE TRIGGER review_events_no_update
BEFORE UPDATE ON review_events BEGIN
    SELECT RAISE(ABORT, 'review_events is append-only');
END;
CREATE TRIGGER review_events_no_delete
BEFORE DELETE ON review_events BEGIN
    SELECT RAISE(ABORT, 'review_events is append-only');
END;
CREATE TRIGGER detections_no_update
BEFORE UPDATE ON detections BEGIN
    SELECT RAISE(ABORT, 'detections are immutable');
END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('review_events',3);
COMMIT;
