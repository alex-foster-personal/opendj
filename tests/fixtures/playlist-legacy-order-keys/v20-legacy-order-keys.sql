BEGIN TRANSACTION;
CREATE TABLE adapters (
        adapter_id   TEXT PRIMARY KEY,
        last_run_at  TEXT,
        last_ok      INTEGER NOT NULL DEFAULT 0,
        notes        TEXT
    );
INSERT INTO "adapters" VALUES('spotify','2026-09-30T02:16:16.400382+00:00',1,'imported playlist FixturePlaylist0000001 snapshot=fixture-snapshot-1');
CREATE TABLE analysis_field_verification (
        source      TEXT NOT NULL,
        field_name  TEXT NOT NULL,
        status      TEXT NOT NULL,
        basis       TEXT NOT NULL CHECK (basis IN
                      ('cross_source','single_source','unverified')),
        normaliser  TEXT,
        checked_at  TEXT,
        verified_by TEXT,
        overridden  INTEGER NOT NULL DEFAULT 0 CHECK (overridden IN (0, 1)),
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (source, field_name)
    );
CREATE TABLE auth_sessions (
        session_token_sha256 TEXT PRIMARY KEY,
        google_sub           TEXT NOT NULL
                               REFERENCES users(google_sub) ON DELETE CASCADE,
        refresh_token        TEXT,
        access_token         TEXT,
        access_expires_at    TEXT,
        created_at           TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL,
        expires_at           TEXT NOT NULL
    );
CREATE TABLE enrollment_grants (
        grant_token_sha256  TEXT PRIMARY KEY,
        google_sub          TEXT NOT NULL
                              REFERENCES users(google_sub) ON DELETE CASCADE,
        created_at          TEXT NOT NULL,
        expires_at          TEXT NOT NULL,
        redeemed_at         TEXT,
        redeemed_machine_id TEXT
    );
CREATE TABLE events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        kind         TEXT NOT NULL,
        stable_id    TEXT,
        payload_json TEXT,
        actor        TEXT
    );
INSERT INTO "events" VALUES(1,'2026-09-30T02:16:16.422238+00:00','playlist.memberships.add',NULL,'{"count":1,"playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d"}','webui');
INSERT INTO "events" VALUES(2,'2026-09-30T02:16:16.422534+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"before":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"command_id":"6f3f534734074e719144c78158486651","op":"memberships","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","ts":"2026-09-30T02:16:16.422527+00:00"}','webui');
INSERT INTO "events" VALUES(3,'2026-09-30T02:16:16.422721+00:00','playlist.memberships.add',NULL,'{"count":1,"playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d"}','webui');
INSERT INTO "events" VALUES(4,'2026-09-30T02:16:16.422817+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"before":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"command_id":"98210920f57f495f9dc04d77d1c097c7","op":"memberships","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","ts":"2026-09-30T02:16:16.422810+00:00"}','webui');
INSERT INTO "events" VALUES(5,'2026-09-30T02:16:16.423022+00:00','playlist.memberships.add',NULL,'{"count":1,"playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d"}','webui');
INSERT INTO "events" VALUES(6,'2026-09-30T02:16:16.423097+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"before":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"command_id":"89d2d3aa6e7f4514ba7dc48107133f70","op":"memberships","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","ts":"2026-09-30T02:16:16.423093+00:00"}','webui');
INSERT INTO "events" VALUES(7,'2026-09-30T02:16:16.423322+00:00','playlist.memberships.remove',NULL,'{"count":1,"playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d"}','webui');
INSERT INTO "events" VALUES(8,'2026-09-30T02:16:16.423496+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"members":[{"item_id":"","order_key":"00000000","position":0,"stable_id":"spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b"},{"item_id":"","order_key":"00000001","position":1,"stable_id":"spotify-pending:596a93f430659989bcb3e0d1349615685587ac40"},{"item_id":"","order_key":"00000002","position":2,"stable_id":"spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c"},{"item_id":"8b701fa57a9f42ceb9afb62170a09afb","order_key":"00000002V","position":12,"stable_id":"spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b"},{"item_id":"","order_key":"00000003","position":3,"stable_id":"spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273"},{"item_id":"","order_key":"00000004","position":4,"stable_id":"spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"},{"item_id":"","order_key":"00000005","position":5,"stable_id":"spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106"},{"item_id":"","order_key":"00000006","position":6,"stable_id":"spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1"},{"item_id":"f2d0c2561bdd468091b6e10ee6eee1bd","order_key":"00000006V","position":13,"stable_id":"spotify-pending:596a93f430659989bcb3e0d1349615685587ac40"},{"item_id":"","order_key":"00000007","position":7,"stable_id":"spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3"},{"item_id":"","order_key":"00000008","position":8,"stable_id":"spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082"},{"item_id":"","order_key":"00000009","position":9,"stable_id":"spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d"},{"item_id":"","order_key":"00000010","position":10,"stable_id":"spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2"},{"item_id":"","order_key":"00000011","position":11,"stable_id":"spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"}],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"before":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"members":[{"item_id":"","order_key":"00000000","position":0,"stable_id":"spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b"},{"item_id":"","order_key":"00000001","position":1,"stable_id":"spotify-pending:596a93f430659989bcb3e0d1349615685587ac40"},{"item_id":"","order_key":"00000002","position":2,"stable_id":"spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c"},{"item_id":"8b701fa57a9f42ceb9afb62170a09afb","order_key":"00000002V","position":12,"stable_id":"spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b"},{"item_id":"","order_key":"00000003","position":3,"stable_id":"spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273"},{"item_id":"","order_key":"00000004","position":4,"stable_id":"spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"},{"item_id":"","order_key":"00000005","position":5,"stable_id":"spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106"},{"item_id":"","order_key":"00000006","position":6,"stable_id":"spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1"},{"item_id":"f2d0c2561bdd468091b6e10ee6eee1bd","order_key":"00000006V","position":13,"stable_id":"spotify-pending:596a93f430659989bcb3e0d1349615685587ac40"},{"item_id":"","order_key":"00000007","position":7,"stable_id":"spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3"},{"item_id":"2ebb59d8640e4f33a0ba1ebcc0ad2f53","order_key":"00000007V","position":14,"stable_id":"spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c"},{"item_id":"","order_key":"00000008","position":8,"stable_id":"spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082"},{"item_id":"","order_key":"00000009","position":9,"stable_id":"spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d"},{"item_id":"","order_key":"00000010","position":10,"stable_id":"spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2"},{"item_id":"","order_key":"00000011","position":11,"stable_id":"spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"}],"name":"Imported from Spotify","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","vendor":"webui","vendor_pl_id":"dc558c1e499e491cabe9c4b8bbfa7f5d"},"command_id":"2b662c1357fd4a75bb070846adcc0926","op":"memberships","playlist_id":"db90fb18832055dac2943cc62e80c5170c10fd5d","ts":"2026-09-30T02:16:16.423490+00:00"}','webui');
INSERT INTO "events" VALUES(9,'2026-09-30T02:16:16.423638+00:00','playlist.insert',NULL,'{"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"}','webui');
INSERT INTO "events" VALUES(10,'2026-09-30T02:16:16.423727+00:00','playlist.edit',NULL,'{"after":{"items":[],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"before":null,"command_id":"815f34a92811497aa0aeb14308c66cdc","op":"create","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","ts":"2026-09-30T02:16:16.423724+00:00"}','webui');
INSERT INTO "events" VALUES(11,'2026-09-30T02:16:16.423860+00:00','playlist.memberships.set',NULL,'{"count":5,"playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"}','webui');
INSERT INTO "events" VALUES(12,'2026-09-30T02:16:16.424015+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"before":{"items":[],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"command_id":"479453be05264bbaa1c83c995dd779b9","op":"memberships","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","ts":"2026-09-30T02:16:16.424012+00:00"}','webui');
INSERT INTO "events" VALUES(13,'2026-09-30T02:16:16.424137+00:00','playlist.memberships.add',NULL,'{"count":1,"playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"}','webui');
INSERT INTO "events" VALUES(14,'2026-09-30T02:16:16.424220+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"before":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"command_id":"b2bcffe02f81419cb847555610249081","op":"memberships","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","ts":"2026-09-30T02:16:16.424217+00:00"}','webui');
INSERT INTO "events" VALUES(15,'2026-09-30T02:16:16.424351+00:00','playlist.memberships.add',NULL,'{"count":1,"playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"}','webui');
INSERT INTO "events" VALUES(16,'2026-09-30T02:16:16.424405+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"before":{"items":["spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Head inserts","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","vendor":"webui","vendor_pl_id":"f59e4fe7e6ba40e384bb3de84a30dc68"},"command_id":"5c0fa43e5f56400ba1acf1f5dd71c49b","op":"memberships","playlist_id":"4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","ts":"2026-09-30T02:16:16.424401+00:00"}','webui');
INSERT INTO "events" VALUES(17,'2026-09-30T02:16:16.424523+00:00','playlist.insert',NULL,'{"name":"Batch append","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","vendor":"webui","vendor_pl_id":"334191c2f7ab4cd68be5f6aff5f632eb"}','webui');
INSERT INTO "events" VALUES(18,'2026-09-30T02:16:16.424577+00:00','playlist.edit',NULL,'{"after":{"items":[],"name":"Batch append","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","vendor":"webui","vendor_pl_id":"334191c2f7ab4cd68be5f6aff5f632eb"},"before":null,"command_id":"11f9a8cc23284f3f976d3aa507cb234a","op":"create","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","ts":"2026-09-30T02:16:16.424574+00:00"}','webui');
INSERT INTO "events" VALUES(19,'2026-09-30T02:16:16.424682+00:00','playlist.memberships.set',NULL,'{"count":5,"playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7"}','webui');
INSERT INTO "events" VALUES(20,'2026-09-30T02:16:16.424804+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Batch append","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","vendor":"webui","vendor_pl_id":"334191c2f7ab4cd68be5f6aff5f632eb"},"before":{"items":[],"name":"Batch append","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","vendor":"webui","vendor_pl_id":"334191c2f7ab4cd68be5f6aff5f632eb"},"command_id":"a5db7bc8dd5c41389c3ab88df1fdff75","op":"memberships","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","ts":"2026-09-30T02:16:16.424801+00:00"}','webui');
INSERT INTO "events" VALUES(21,'2026-09-30T02:16:16.425054+00:00','playlist.memberships.add',NULL,'{"count":60,"playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7"}','webui');
INSERT INTO "events" VALUES(22,'2026-09-30T02:16:16.425539+00:00','playlist.edit',NULL,'{"after":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556","spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"],"name":"Batch append","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","vendor":"webui","vendor_pl_id":"334191c2f7ab4cd68be5f6aff5f632eb"},"before":{"items":["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"],"name":"Batch append","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","vendor":"webui","vendor_pl_id":"334191c2f7ab4cd68be5f6aff5f632eb"},"command_id":"652ee48bc3a640e68a854dca23c70d8f","op":"memberships","playlist_id":"bb1475472f71b996e57e347a95860e31da5caea7","ts":"2026-09-30T02:16:16.425535+00:00"}','webui');
CREATE TABLE feedback_pins (
        pin_id           TEXT PRIMARY KEY CHECK (length(pin_id) > 0),
        doc              TEXT NOT NULL CHECK (json_valid(doc)),
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT,
        deleted_at       TEXT
    );
CREATE TABLE hub_changelog (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name       TEXT NOT NULL,
        row_pk           TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT NOT NULL,
        received_at      TEXT NOT NULL
    );
CREATE TABLE local_changelog (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name       TEXT NOT NULL,
        row_pk           TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT NOT NULL,
        received_at      TEXT NOT NULL
    );
INSERT INTO "local_changelog" VALUES(1,'playlists','["spotify:FixturePlaylist0000001"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(2,'tracks','["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(3,'track_vendor_ids','["spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(4,'tracks','["spotify-pending:596a93f430659989bcb3e0d1349615685587ac40"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(5,'track_vendor_ids','["spotify-pending:596a93f430659989bcb3e0d1349615685587ac40","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(6,'tracks','["spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(7,'track_vendor_ids','["spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(8,'tracks','["spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(9,'track_vendor_ids','["spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(10,'tracks','["spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(11,'track_vendor_ids','["spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(12,'tracks','["spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(13,'track_vendor_ids','["spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(14,'tracks','["spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(15,'track_vendor_ids','["spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(16,'tracks','["spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(17,'track_vendor_ids','["spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(18,'tracks','["spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(19,'track_vendor_ids','["spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(20,'tracks','["spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(21,'track_vendor_ids','["spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(22,'tracks','["spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(23,'track_vendor_ids','["spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(24,'tracks','["spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(25,'track_vendor_ids','["spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556","spotify"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(26,'playlist_memberships','["spotify:FixturePlaylist0000001","0"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(27,'playlist_memberships','["spotify:FixturePlaylist0000001","1"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(28,'playlist_memberships','["spotify:FixturePlaylist0000001","2"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(29,'playlist_memberships','["spotify:FixturePlaylist0000001","3"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(30,'playlist_memberships','["spotify:FixturePlaylist0000001","4"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(31,'playlist_memberships','["spotify:FixturePlaylist0000001","5"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(32,'playlist_memberships','["spotify:FixturePlaylist0000001","6"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(33,'playlist_memberships','["spotify:FixturePlaylist0000001","7"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(34,'playlist_memberships','["spotify:FixturePlaylist0000001","8"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(35,'playlist_memberships','["spotify:FixturePlaylist0000001","9"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(36,'playlist_memberships','["spotify:FixturePlaylist0000001","10"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(37,'playlist_memberships','["spotify:FixturePlaylist0000001","11"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(38,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","0"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(39,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","1"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(40,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","2"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(41,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","3"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(42,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","4"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(43,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","5"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(44,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","6"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(45,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","7"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(46,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","8"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(47,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","9"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(48,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","10"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(49,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","11"]','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400382+00:00');
INSERT INTO "local_changelog" VALUES(50,'playlists','["db90fb18832055dac2943cc62e80c5170c10fd5d"]','2026-09-30T02:16:16.400383+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.400383+00:00');
INSERT INTO "local_changelog" VALUES(51,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","12"]','2026-09-30T02:16:16.422238+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.422238+00:00');
INSERT INTO "local_changelog" VALUES(52,'playlists','["db90fb18832055dac2943cc62e80c5170c10fd5d"]','2026-09-30T02:16:16.422238+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.422238+00:00');
INSERT INTO "local_changelog" VALUES(53,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","13"]','2026-09-30T02:16:16.422721+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.422721+00:00');
INSERT INTO "local_changelog" VALUES(54,'playlists','["db90fb18832055dac2943cc62e80c5170c10fd5d"]','2026-09-30T02:16:16.422721+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.422721+00:00');
INSERT INTO "local_changelog" VALUES(55,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","14"]','2026-09-30T02:16:16.423022+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423022+00:00');
INSERT INTO "local_changelog" VALUES(56,'playlists','["db90fb18832055dac2943cc62e80c5170c10fd5d"]','2026-09-30T02:16:16.423022+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423022+00:00');
INSERT INTO "local_changelog" VALUES(57,'playlist_memberships','["db90fb18832055dac2943cc62e80c5170c10fd5d","14"]','2026-09-30T02:16:16.423322+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423322+00:00');
INSERT INTO "local_changelog" VALUES(58,'playlists','["db90fb18832055dac2943cc62e80c5170c10fd5d"]','2026-09-30T02:16:16.423322+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423322+00:00');
INSERT INTO "local_changelog" VALUES(59,'playlists','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"]','2026-09-30T02:16:16.423638+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423638+00:00');
INSERT INTO "local_changelog" VALUES(60,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","0"]','2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423860+00:00');
INSERT INTO "local_changelog" VALUES(61,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","1"]','2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423860+00:00');
INSERT INTO "local_changelog" VALUES(62,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","2"]','2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423860+00:00');
INSERT INTO "local_changelog" VALUES(63,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","3"]','2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423860+00:00');
INSERT INTO "local_changelog" VALUES(64,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","4"]','2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423860+00:00');
INSERT INTO "local_changelog" VALUES(65,'playlists','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"]','2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423860+00:00');
INSERT INTO "local_changelog" VALUES(66,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","5"]','2026-09-30T02:16:16.424137+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424137+00:00');
INSERT INTO "local_changelog" VALUES(67,'playlists','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"]','2026-09-30T02:16:16.424137+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424137+00:00');
INSERT INTO "local_changelog" VALUES(68,'playlist_memberships','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5","6"]','2026-09-30T02:16:16.424351+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424351+00:00');
INSERT INTO "local_changelog" VALUES(69,'playlists','["4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5"]','2026-09-30T02:16:16.424351+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424351+00:00');
INSERT INTO "local_changelog" VALUES(70,'playlists','["bb1475472f71b996e57e347a95860e31da5caea7"]','2026-09-30T02:16:16.424523+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424523+00:00');
INSERT INTO "local_changelog" VALUES(71,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","0"]','2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424682+00:00');
INSERT INTO "local_changelog" VALUES(72,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","1"]','2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424682+00:00');
INSERT INTO "local_changelog" VALUES(73,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","2"]','2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424682+00:00');
INSERT INTO "local_changelog" VALUES(74,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","3"]','2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424682+00:00');
INSERT INTO "local_changelog" VALUES(75,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","4"]','2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424682+00:00');
INSERT INTO "local_changelog" VALUES(76,'playlists','["bb1475472f71b996e57e347a95860e31da5caea7"]','2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.424682+00:00');
INSERT INTO "local_changelog" VALUES(77,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","5"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(78,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","6"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(79,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","7"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(80,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","8"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(81,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","9"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(82,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","10"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(83,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","11"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(84,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","12"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(85,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","13"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(86,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","14"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(87,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","15"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(88,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","16"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(89,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","17"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(90,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","18"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(91,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","19"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(92,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","20"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(93,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","21"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(94,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","22"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(95,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","23"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(96,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","24"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(97,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","25"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(98,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","26"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(99,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","27"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(100,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","28"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(101,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","29"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(102,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","30"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(103,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","31"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(104,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","32"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(105,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","33"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(106,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","34"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(107,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","35"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(108,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","36"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(109,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","37"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(110,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","38"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(111,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","39"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(112,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","40"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(113,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","41"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(114,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","42"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(115,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","43"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(116,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","44"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(117,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","45"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(118,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","46"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(119,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","47"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(120,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","48"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(121,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","49"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(122,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","50"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(123,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","51"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(124,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","52"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(125,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","53"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(126,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","54"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(127,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","55"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(128,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","56"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(129,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","57"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(130,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","58"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(131,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","59"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(132,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","60"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(133,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","61"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(134,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","62"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(135,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","63"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(136,'playlist_memberships','["bb1475472f71b996e57e347a95860e31da5caea7","64"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
INSERT INTO "local_changelog" VALUES(137,'playlists','["bb1475472f71b996e57e347a95860e31da5caea7"]','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.425054+00:00');
CREATE TABLE lyric_verdict (
        stable_id          TEXT PRIMARY KEY REFERENCES tracks(stable_id) ON DELETE CASCADE,
        verdict            TEXT NOT NULL CHECK (verdict IN
                             ('vocal','sparse','no-lyrics','unknown')),
        coverage_pct       REAL,
        source             TEXT,
        language_iso3      TEXT,
        n_words            INTEGER,
        n_lines            INTEGER,
        pct_witness_red    REAL,
        override           TEXT CHECK (override IS NULL OR override IN
                             ('vocal','sparse','no-lyrics')),
        override_note      TEXT,
        pipeline_version   TEXT NOT NULL,
        words_content_hash TEXT CHECK (words_content_hash IS NULL OR
                             length(words_content_hash) = 64),
        computed_at        TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        origin_device_id   TEXT,
        deleted_at         TEXT
    );
CREATE TABLE machine_credentials (
        machine_id         TEXT PRIMARY KEY
                             REFERENCES machines(machine_id) ON DELETE CASCADE,
        credential_sha256  TEXT NOT NULL UNIQUE,
        minted_at          TEXT NOT NULL
    );
CREATE TABLE machine_owners (
        machine_id      TEXT PRIMARY KEY
                          REFERENCES machines(machine_id) ON DELETE CASCADE,
        google_sub      TEXT NOT NULL
                          REFERENCES users(google_sub) ON DELETE CASCADE,
        hub_machine_id  TEXT NOT NULL,
        enrolled_at     TEXT NOT NULL,
        enrolled_via    TEXT NOT NULL CHECK
                          (enrolled_via IN ('grant','google_id_token','adopt')),
        revoked_at      TEXT
    );
CREATE TABLE machines (
        machine_id  TEXT PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        platform    TEXT NOT NULL CHECK
                      (platform IN ('macos','windows','linux')),
        is_hub      INTEGER NOT NULL DEFAULT 0,
        data_root   TEXT,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL
    );
INSERT INTO "machines" VALUES('071134cb6e454dee861fd215539aad79','fixture-host','macos',0,'/Users/dev/opendj-data','2026-09-30T02:16:16.400335+00:00','2026-09-30T02:16:16.400335+00:00');
CREATE TABLE path_availability (
        resolver_namespace TEXT NOT NULL,
        logical_path       TEXT NOT NULL,
        materialised_size  INTEGER,
        checked_at         TEXT NOT NULL,
        PRIMARY KEY (resolver_namespace, logical_path)
    );
CREATE TABLE pending_tracks (
    pending_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id           TEXT NOT NULL REFERENCES playlists(playlist_id)
                            ON DELETE CASCADE,
    position              INTEGER NOT NULL,
    spotify_uri           TEXT NOT NULL,
    isrc                  TEXT,
    title                 TEXT NOT NULL,
    artist                TEXT NOT NULL,
    album                 TEXT,
    duration_ms           INTEGER,
    suggested_sources_json TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN
                             ('pending','purchased','resolved','abandoned')),
    added_at              TEXT NOT NULL,
    resolved_stable_id    TEXT REFERENCES tracks(stable_id) ON DELETE SET NULL,
    resolved_at           TEXT
);
INSERT INTO "pending_tracks" VALUES(1,'spotify:FixturePlaylist0000001',0,'spotify:track:FixtureTrack0000000000',NULL,'Fixture Track 00','Fixture Artist','Fixture Album',180000,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+00", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+00&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+00", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+00", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+00&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(2,'spotify:FixturePlaylist0000001',1,'spotify:track:FixtureTrack0000000001',NULL,'Fixture Track 01','Fixture Artist','Fixture Album',180001,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+01", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+01&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+01", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+01", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+01&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(3,'spotify:FixturePlaylist0000001',2,'spotify:track:FixtureTrack0000000002',NULL,'Fixture Track 02','Fixture Artist','Fixture Album',180002,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+02", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+02&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+02", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+02", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+02&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(4,'spotify:FixturePlaylist0000001',3,'spotify:track:FixtureTrack0000000003',NULL,'Fixture Track 03','Fixture Artist','Fixture Album',180003,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+03", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+03&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+03", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+03", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+03&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(5,'spotify:FixturePlaylist0000001',4,'spotify:track:FixtureTrack0000000004',NULL,'Fixture Track 04','Fixture Artist','Fixture Album',180004,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+04", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+04&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+04", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+04", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+04&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(6,'spotify:FixturePlaylist0000001',5,'spotify:track:FixtureTrack0000000005',NULL,'Fixture Track 05','Fixture Artist','Fixture Album',180005,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+05", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+05&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+05", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+05", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+05&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(7,'spotify:FixturePlaylist0000001',6,'spotify:track:FixtureTrack0000000006',NULL,'Fixture Track 06','Fixture Artist','Fixture Album',180006,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+06", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+06&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+06", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+06", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+06&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(8,'spotify:FixturePlaylist0000001',7,'spotify:track:FixtureTrack0000000007',NULL,'Fixture Track 07','Fixture Artist','Fixture Album',180007,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+07", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+07&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+07", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+07", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+07&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(9,'spotify:FixturePlaylist0000001',8,'spotify:track:FixtureTrack0000000008',NULL,'Fixture Track 08','Fixture Artist','Fixture Album',180008,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+08", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+08&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+08", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+08", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+08&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(10,'spotify:FixturePlaylist0000001',9,'spotify:track:FixtureTrack0000000009',NULL,'Fixture Track 09','Fixture Artist','Fixture Album',180009,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+09", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+09&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+09", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+09", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+09&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(11,'spotify:FixturePlaylist0000001',10,'spotify:track:FixtureTrack0000000010',NULL,'Fixture Track 10','Fixture Artist','Fixture Album',180010,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+10", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+10&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+10", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+10", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+10&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
INSERT INTO "pending_tracks" VALUES(12,'spotify:FixturePlaylist0000001',11,'spotify:track:FixtureTrack0000000011',NULL,'Fixture Track 11','Fixture Artist','Fixture Album',180011,'{"beatport": "https://www.beatport.com/search?q=Fixture+Artist+Fixture+Track+11", "bandcamp": "https://bandcamp.com/search?q=Fixture+Artist+Fixture+Track+11&item_type=t", "qobuz": "https://www.qobuz.com/us-en/search?q=Fixture+Artist+Fixture+Track+11", "apple_music": "https://music.apple.com/us/search?term=Fixture+Artist+Fixture+Track+11", "discogs": "https://www.discogs.com/search?q=Fixture+Artist+Fixture+Track+11&type=release"}','pending','2026-09-30T02:16:16.400382+00:00',NULL,NULL);
CREATE TABLE playlist_memberships (
        playlist_id  TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        position     INTEGER NOT NULL, updated_at TEXT, origin_device_id TEXT, deleted_at TEXT, item_id TEXT, order_key TEXT,
        PRIMARY KEY (playlist_id, position)
    );
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',0,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',1,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',2,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',3,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',4,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',5,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',6,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',7,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',8,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',9,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',10,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('spotify:FixturePlaylist0000001','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',11,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',0,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',1,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',2,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',3,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',4,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',5,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',6,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',7,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',8,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',9,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',10,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',11,'2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL,NULL);
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',12,'2026-09-30T02:16:16.422238+00:00','071134cb6e454dee861fd215539aad79',NULL,'8b701fa57a9f42ceb9afb62170a09afb','00000002V');
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',13,'2026-09-30T02:16:16.422721+00:00','071134cb6e454dee861fd215539aad79',NULL,'f2d0c2561bdd468091b6e10ee6eee1bd','00000006V');
INSERT INTO "playlist_memberships" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',14,'2026-09-30T02:16:16.423322+00:00','071134cb6e454dee861fd215539aad79','2026-09-30T02:16:16.423322+00:00','2ebb59d8640e4f33a0ba1ebcc0ad2f53','00000007V');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',0,'2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79',NULL,'e2b0e0e3fddd4b64b0bc42d5396e3dd7','00000000');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',1,'2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79',NULL,'8e40830d2e644570bd91992e42985b42','00000001');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',2,'2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79',NULL,'3df0fb2676aa4f2d8922b09653a52a88','00000002');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',3,'2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79',NULL,'c66f6704378b44c0ba55f6c9cc8879fa','00000003');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',4,'2026-09-30T02:16:16.423860+00:00','071134cb6e454dee861fd215539aad79',NULL,'e2d2428e69e24f02b5a9e07a3cf820f6','00000004');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',5,'2026-09-30T02:16:16.424137+00:00','071134cb6e454dee861fd215539aad79',NULL,'22c27d7cb7d54152a2401bce1231e942','');
INSERT INTO "playlist_memberships" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',6,'2026-09-30T02:16:16.424351+00:00','071134cb6e454dee861fd215539aad79',NULL,'72135aeb11124c00899e1fabba9f1ff4','00000004');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',0,'2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79',NULL,'d8acb84b66f74955961a7e72a839fe32','00000000');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',1,'2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79',NULL,'a0f9cf3d951f48548ff97395b9451133','00000001');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',2,'2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79',NULL,'689a5e4262cb4d7bb1ccc6bb0c21a534','00000002');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',3,'2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79',NULL,'177a011d379a4b98aca14ea8c6f7d428','00000003');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',4,'2026-09-30T02:16:16.424682+00:00','071134cb6e454dee861fd215539aad79',NULL,'0d61ea0257244ecdbb6f31973d5c0174','00000004');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',5,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'0cd8a2bf937045619f2abd741d56f39c','00000004V');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',6,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f2c8b28bd07347fcaeba57690f42ceeb','00000004VV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',7,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'9f640709bbcc42be8db658287376f666','00000004VVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',8,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'cdec3a4f904d4fd390b3f39127582889','00000004VVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',9,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'28b9c6e819794f618a5744257eac05af','00000004VVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',10,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'88f88a5b505142548a6cea388e0c1a7e','00000004VVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',11,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'37e60a4874d243898440bbea28b6e390','00000004VVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',12,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'56029959e05d4aeab394fccd64c1ac93','00000004VVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',13,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'fc7b85ef66bc416693d66c4d55f13731','00000004VVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',14,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'983a62c1f6a84837b7c22d8111b9d395','00000004VVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',15,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'a5cf266d6aa54479ad93a645b4bdf551','00000004VVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',16,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f768781161e84c59a600df840361c200','00000004VVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',17,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'9802988cdaad4bfa97f1a13f9f26f5d2','00000004VVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',18,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'16afe6429a994460a024414e078b0265','00000004VVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',19,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'e9ce2e0159a54b36ac27995f129a3ab7','00000004VVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',20,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'32e4fe180d98426a90e1e541fb0af935','00000004VVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',21,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f2d195ad588243d7b2867967dd5a2e18','00000004VVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',22,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'31e4e6264fbf4dd88f69441ab73a7013','00000004VVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',23,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'9be62d7cd4c14476a6c0d31df81be174','00000004VVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',24,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'de14a46b20da460d8a1a40ddef32d061','00000004VVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',25,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'2fc96db23b944a3ab30b045c7fbc5174','00000004VVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',26,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'638ae2ad57c6479994f6e4472ebccabc','00000004VVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',27,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'224697536a9c4c8e8e5b28eb2af65400','00000004VVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',28,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'0d8921f3c6fe45b98c2185f480487841','00000004VVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',29,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'c9c0ff030e1e45a5b8821b996b13427a','00000004VVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',30,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'1afb42edf73041f982ed19a22114d301','00000004VVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',31,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'a67fba95ecc44c84b2eedb04c7db4b15','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',32,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'13616e5672274b2babbe71a6b70f86a0','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',33,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'451f3254aa354811a6b0ccf0817db392','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',34,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f24ef5cf8d7b4616bc8a44cd26e4535d','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',35,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'560d90a1ca044afdb97adecd71b38663','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',36,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'bc51a71fa27f4766b6b6e2f5cb7c151c','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',37,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'c13f0b5b79c94d1c962cf70e314b3acf','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',38,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'180f310d500f41f6863601b48c80232b','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',39,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f2f1dd8a71e942acad691b463f6ef724','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',40,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f73f52480b264a39a25ab180fbf13044','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',41,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'12ec21d0ecfa41b7954cda066e19acb1','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',42,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'747b25083f9346df9276c6bc4764ea76','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',43,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'bdff1d2a4942497386ce2363b177cc4a','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',44,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'57f1ae81a2ba4891b87f3596071d464a','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',45,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'cb9c642859a24b018a9936bc20433dbf','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',46,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'a34875f0dd334ebd884f062e1780eabb','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',47,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'9380bcff136f4f3e881b884c32d84f93','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',48,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f4420eb96e944dee97c3c487d05ca1a7','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',49,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'05ba35c0b1674525be973ea8357dd912','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',50,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'dde1398823114572bbc33f8671a9219d','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',51,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'6c14628eb1ab41d186b4201930898e26','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',52,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'ef7a57a7ba864985bdc6dd61b2abd2eb','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b',53,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'a043144c683242038a80677602ddf8a4','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:596a93f430659989bcb3e0d1349615685587ac40',54,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'cf745d8979b24c3aa4f79d0aeb8fd84f','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c',55,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'7d2692677fdd48eb8773297b3c344670','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273',56,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'ca86328741a84b4e8b427d07674f2915','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b',57,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'bf439805e0904fe8bd2ca8438dc4f9c6','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106',58,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'ce501d90fb50450c960cc149ebaa3afd','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1',59,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'8d8f1b9775224ed7b4c719c39b5fb147','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3',60,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'a8a644b8208c4105a1f4b250ade738c0','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082',61,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'95c16b3ad1834b53ba27de848efe8f42','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d',62,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'f154d5698bcf4e93bab13e69799fa09a','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2',63,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'1bd8758dbeae48caad99a3eaeb95ed6f','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
INSERT INTO "playlist_memberships" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556',64,'2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,'6123307904094f1eb19b3d7855134a82','00000004VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV');
CREATE TABLE playlist_pins (
        machine_id       TEXT NOT NULL
                           REFERENCES machines(machine_id) ON DELETE CASCADE,
        playlist_id      TEXT NOT NULL
                           REFERENCES playlists(playlist_id) ON DELETE CASCADE,
        mode             TEXT NOT NULL CHECK (mode IN
                           ('pinned','cached','stream','excluded')),
        updated_at       TEXT,
        origin_device_id TEXT,
        deleted_at       TEXT,
        PRIMARY KEY (machine_id, playlist_id)
    );
CREATE TABLE playlists (
        playlist_id   TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        vendor        TEXT NOT NULL,
        vendor_pl_id  TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL, origin_device_id TEXT, deleted_at TEXT, forbid_duplicates INTEGER NOT NULL DEFAULT 0,
        UNIQUE (vendor, vendor_pl_id)
    );
INSERT INTO "playlists" VALUES('spotify:FixturePlaylist0000001','Imported from Spotify','spotify','FixturePlaylist0000001','2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,0);
INSERT INTO "playlists" VALUES('db90fb18832055dac2943cc62e80c5170c10fd5d','Imported from Spotify','webui','dc558c1e499e491cabe9c4b8bbfa7f5d','2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.423322+00:00','071134cb6e454dee861fd215539aad79',NULL,0);
INSERT INTO "playlists" VALUES('4e53ab7ed1960050c2cebcc9413bdf24dadbc6e5','Head inserts','webui','f59e4fe7e6ba40e384bb3de84a30dc68','2026-09-30T02:16:16.423638+00:00','2026-09-30T02:16:16.424351+00:00','071134cb6e454dee861fd215539aad79',NULL,0);
INSERT INTO "playlists" VALUES('bb1475472f71b996e57e347a95860e31da5caea7','Batch append','webui','334191c2f7ab4cd68be5f6aff5f632eb','2026-09-30T02:16:16.424523+00:00','2026-09-30T02:16:16.425054+00:00','071134cb6e454dee861fd215539aad79',NULL,0);
CREATE TABLE schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        );
INSERT INTO "schema_meta" VALUES(1,'2026-09-30T02:16:16.343760+00:00');
INSERT INTO "schema_meta" VALUES(2,'2026-09-30T02:16:16.344526+00:00');
INSERT INTO "schema_meta" VALUES(3,'2026-09-30T02:16:16.345045+00:00');
INSERT INTO "schema_meta" VALUES(4,'2026-09-30T02:16:16.345310+00:00');
INSERT INTO "schema_meta" VALUES(5,'2026-09-30T02:16:16.345422+00:00');
INSERT INTO "schema_meta" VALUES(6,'2026-09-30T02:16:16.345574+00:00');
INSERT INTO "schema_meta" VALUES(7,'2026-09-30T02:16:16.348024+00:00');
INSERT INTO "schema_meta" VALUES(8,'2026-09-30T02:16:16.348628+00:00');
INSERT INTO "schema_meta" VALUES(9,'2026-09-30T02:16:16.348874+00:00');
INSERT INTO "schema_meta" VALUES(10,'2026-09-30T02:16:16.349957+00:00');
INSERT INTO "schema_meta" VALUES(11,'2026-09-30T02:16:16.350200+00:00');
INSERT INTO "schema_meta" VALUES(12,'2026-09-30T02:16:16.350301+00:00');
INSERT INTO "schema_meta" VALUES(13,'2026-09-30T02:16:16.351856+00:00');
INSERT INTO "schema_meta" VALUES(14,'2026-09-30T02:16:16.352456+00:00');
INSERT INTO "schema_meta" VALUES(15,'2026-09-30T02:16:16.352521+00:00');
INSERT INTO "schema_meta" VALUES(16,'2026-09-30T02:16:16.352618+00:00');
INSERT INTO "schema_meta" VALUES(17,'2026-09-30T02:16:16.352680+00:00');
INSERT INTO "schema_meta" VALUES(18,'2026-09-30T02:16:16.353073+00:00');
INSERT INTO "schema_meta" VALUES(19,'2026-09-30T02:16:16.353766+00:00');
INSERT INTO "schema_meta" VALUES(20,'2026-09-30T02:16:16.354018+00:00');
CREATE TABLE schema_meta_markers (
        marker     TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    );
INSERT INTO "schema_meta_markers" VALUES('v15_track_fields_stamp_backfill','2026-09-30T02:16:16.354217+00:00');
INSERT INTO "schema_meta_markers" VALUES('agents_md_generated:v1:sqliteschema90:fp72860b1c9f28bfdb:adapters,analysis_field_verification,auth_sessions,enrollment_grants,events,feedback_pins,hub_changelog,launcher_meta,local_changelog,lyric_verdict,machine_credentials,machine_owners,machines,pairing_alignments,pairing_capture_schema_meta,pairing_sync_snapshots,pairings,path_availability,play_order_entries,play_orders,play_orders_schema_meta,playlist_memberships,playlist_pins,playlist_set_entries,playlist_set_runs,playlist_sets,playlist_sets_schema_meta,playlists,schema_meta,schema_meta_markers,smartlists,sync_identity_remap,sync_policies,sync_state,track_availability,track_energy_segments,track_field_history,track_fields,track_locations,track_vendor_ids,tracks,tracks_frecency,tracks_fts,tracks_fts_config,tracks_fts_content,tracks_fts_data,tracks_fts_docsize,tracks_fts_idx,unmatched_source_analysis,users','2026-09-30T02:16:16.393695+00:00');
INSERT INTO "schema_meta_markers" VALUES('agents_md_generated:v1:sqliteschema97:fp08ecc9e32e57bcb5:adapters,analysis_field_verification,auth_sessions,enrollment_grants,events,feedback_pins,hub_changelog,launcher_meta,local_changelog,lyric_verdict,machine_credentials,machine_owners,machines,pairing_alignments,pairing_capture_schema_meta,pairing_sync_snapshots,pairings,path_availability,play_order_entries,play_orders,play_orders_schema_meta,playlist_memberships,playlist_pins,playlist_set_entries,playlist_set_runs,playlist_sets,playlist_sets_schema_meta,playlists,schema_meta,schema_meta_markers,smartlists,sync_identity_remap,sync_policies,sync_state,track_availability,track_energy_segments,track_field_history,track_fields,track_locations,track_vendor_ids,tracks,tracks_frecency,tracks_fts,tracks_fts_config,tracks_fts_content,tracks_fts_data,tracks_fts_docsize,tracks_fts_idx,unmatched_source_analysis,users','2026-09-30T02:16:16.421830+00:00');
CREATE TABLE spotify_playlist_links (
    vendor_pl_id          TEXT PRIMARY KEY,
    spotify_playlist_id   TEXT NOT NULL REFERENCES playlists(playlist_id)
                            ON DELETE CASCADE,
    odj_playlist_id       TEXT NOT NULL REFERENCES playlists(playlist_id)
                            ON DELETE CASCADE,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
INSERT INTO "spotify_playlist_links" VALUES('FixturePlaylist0000001','spotify:FixturePlaylist0000001','db90fb18832055dac2943cc62e80c5170c10fd5d','2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00');
CREATE TABLE spotify_playlist_meta (
    playlist_id      TEXT PRIMARY KEY REFERENCES playlists(playlist_id)
                       ON DELETE CASCADE,
    vendor_pl_id     TEXT NOT NULL,
    snapshot_id      TEXT NOT NULL,
    track_count      INTEGER NOT NULL,
    matched_count    INTEGER NOT NULL,
    pending_count    INTEGER NOT NULL,
    last_import_at   TEXT NOT NULL
);
INSERT INTO "spotify_playlist_meta" VALUES('spotify:FixturePlaylist0000001','FixturePlaylist0000001','fixture-snapshot-1',12,0,12,'2026-09-30T02:16:16.400382+00:00');
CREATE TABLE "sync_policies" (
        machine_id       TEXT NOT NULL
                           REFERENCES machines(machine_id) ON DELETE CASCADE,
        asset_kind       TEXT NOT NULL CHECK (asset_kind IN
                           ('audio','stem_bundle','anlz_cache','vocal_cache','lyrics_cache','karaoke_words')),
        mode             TEXT NOT NULL CHECK (mode IN
                           ('pinned','cached','stream','excluded')),
        cache_budget_mb  INTEGER,
        updated_at       TEXT,
        origin_device_id TEXT,
        deleted_at       TEXT,
        PRIMARY KEY (machine_id, asset_kind)
    );
CREATE TABLE sync_state (
        peer            TEXT PRIMARY KEY,
        last_push_seq   INTEGER NOT NULL DEFAULT 0,
        last_pull_seq   INTEGER NOT NULL DEFAULT 0,
        last_sync_at    TEXT,
        peer_generation TEXT
    );
CREATE TABLE track_availability (
        stable_id     TEXT PRIMARY KEY REFERENCES tracks(stable_id) ON DELETE CASCADE,
        state         TEXT NOT NULL CHECK (state IN
                        ('present','absent','awaiting_volume','streaming')),
        checked_path  TEXT,
        checked_at    TEXT NOT NULL
    );
CREATE TABLE track_energy_segments (
        stable_id   TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        start_ms    INTEGER NOT NULL CHECK (start_ms >= 0),
        length_ms   INTEGER NOT NULL CHECK (length_ms > 0),
        energy      INTEGER NOT NULL CHECK (energy BETWEEN 1 AND 10),
        source      TEXT NOT NULL CHECK (source IN
                      ('mik','rekordbox','djay','serato','traktor',
                       'open-dj-tool','manual','inferred','webui')),
        confidence  REAL CHECK (confidence IS NULL OR
                                (confidence >= 0 AND confidence <= 1)),
        start_clamped INTEGER NOT NULL DEFAULT 0
                        CHECK (start_clamped IN (0, 1)),
        modified_at TEXT NOT NULL,
        PRIMARY KEY (stable_id, source, seq)
    );
CREATE TABLE "track_field_history" (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_id     TEXT NOT NULL,
        field_name    TEXT NOT NULL,
        value_json    TEXT NOT NULL,
        source        TEXT NOT NULL,
        confidence    REAL,
        modified_at   TEXT NOT NULL,
        superseded_at TEXT NOT NULL
    );
CREATE TABLE "track_fields" (
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL CHECK (source IN
                       ('mik','rekordbox','djay','serato','traktor',
                        'open-dj-tool','manual','inferred','webui')),
        confidence   REAL CHECK (confidence IS NULL OR
                                 (confidence >= 0 AND confidence <= 1)),
        modified_at  TEXT NOT NULL, updated_at TEXT, origin_device_id TEXT, deleted_at TEXT,
        PRIMARY KEY (stable_id, field_name)
    );
CREATE TABLE "track_locations" (
        location_id      TEXT PRIMARY KEY NOT NULL
                           DEFAULT (lower(hex(randomblob(16)))),
        stable_id        TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        machine_id       TEXT REFERENCES machines(machine_id),
        kind             TEXT NOT NULL CHECK (kind IN ('local', 'remote')),
        role             TEXT NOT NULL DEFAULT 'alternate'
                           CHECK (role IN ('primary', 'alternate')),
        file_path        TEXT,
        remote_url       TEXT,
        venue_key        TEXT,
        venue_rank       INTEGER,
        available        INTEGER NOT NULL DEFAULT 0,
        probed_at        TEXT,
        content_hash     TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT,
        deleted_at       TEXT,
        CHECK (
            (file_path IS NOT NULL AND file_path != '')
            OR (remote_url IS NOT NULL AND remote_url != '')
        )
    );
CREATE TABLE track_vendor_ids (
        stable_id  TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        vendor     TEXT NOT NULL,
        vendor_id  TEXT NOT NULL, updated_at TEXT, origin_device_id TEXT, deleted_at TEXT,
        PRIMARY KEY (stable_id, vendor)
    );
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b','spotify','FixtureTrack0000000000','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:596a93f430659989bcb3e0d1349615685587ac40','spotify','FixtureTrack0000000001','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c','spotify','FixtureTrack0000000002','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273','spotify','FixtureTrack0000000003','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b','spotify','FixtureTrack0000000004','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106','spotify','FixtureTrack0000000005','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1','spotify','FixtureTrack0000000006','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3','spotify','FixtureTrack0000000007','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082','spotify','FixtureTrack0000000008','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d','spotify','FixtureTrack0000000009','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2','spotify','FixtureTrack0000000010','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
INSERT INTO "track_vendor_ids" VALUES('spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556','spotify','FixtureTrack0000000011','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL);
CREATE TABLE tracks (
        stable_id       TEXT PRIMARY KEY,
        stable_id_tier  TEXT NOT NULL CHECK
                          (stable_id_tier IN ('isrc','fingerprint','inferred')),
        title           TEXT,
        artists_json    TEXT,
        album           TEXT,
        isrc            TEXT,
        duration_ms     INTEGER,
        file_path       TEXT,
        content_hash    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    , origin_device_id TEXT, deleted_at TEXT, audio_hash TEXT);
INSERT INTO "tracks" VALUES('spotify-pending:100c93f7f3a9622e773abaac21fd5f8610ca018b','inferred','Fixture Track 00','["Fixture Artist"]','Fixture Album',NULL,180000,'spotify:track:FixtureTrack0000000000',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:596a93f430659989bcb3e0d1349615685587ac40','inferred','Fixture Track 01','["Fixture Artist"]','Fixture Album',NULL,180001,'spotify:track:FixtureTrack0000000001',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:be0f9e19e5a47e5aed754a66e33f3d164d35194c','inferred','Fixture Track 02','["Fixture Artist"]','Fixture Album',NULL,180002,'spotify:track:FixtureTrack0000000002',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:87a70525439359ddc2e2684bf9340f00ccf58273','inferred','Fixture Track 03','["Fixture Artist"]','Fixture Album',NULL,180003,'spotify:track:FixtureTrack0000000003',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:b2337cd86360f1b91a6726f6058fe3701934895b','inferred','Fixture Track 04','["Fixture Artist"]','Fixture Album',NULL,180004,'spotify:track:FixtureTrack0000000004',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:0a16de317fbd50a908f86c86de3e3258bb923106','inferred','Fixture Track 05','["Fixture Artist"]','Fixture Album',NULL,180005,'spotify:track:FixtureTrack0000000005',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:345b249214c3c5a02f5b57698debae081226b5a1','inferred','Fixture Track 06','["Fixture Artist"]','Fixture Album',NULL,180006,'spotify:track:FixtureTrack0000000006',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:39a6b2f38e1e2db7f29e72438820b9ef73391dc3','inferred','Fixture Track 07','["Fixture Artist"]','Fixture Album',NULL,180007,'spotify:track:FixtureTrack0000000007',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:528e6f2c114cd419c0fc129abc280f719b084082','inferred','Fixture Track 08','["Fixture Artist"]','Fixture Album',NULL,180008,'spotify:track:FixtureTrack0000000008',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:8f18f5a7e75e5b970cf0bfe7cd75bb50566aaa5d','inferred','Fixture Track 09','["Fixture Artist"]','Fixture Album',NULL,180009,'spotify:track:FixtureTrack0000000009',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:3f9161c8aeb72b615771f266ee4e7af40ca1f4c2','inferred','Fixture Track 10','["Fixture Artist"]','Fixture Album',NULL,180010,'spotify:track:FixtureTrack0000000010',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
INSERT INTO "tracks" VALUES('spotify-pending:c2401058e3fa4369f1d994f258d3a29ffcdab556','inferred','Fixture Track 11','["Fixture Artist"]','Fixture Album',NULL,180011,'spotify:track:FixtureTrack0000000011',NULL,'2026-09-30T02:16:16.400382+00:00','2026-09-30T02:16:16.400382+00:00','071134cb6e454dee861fd215539aad79',NULL,NULL);
CREATE TABLE unmatched_source_analysis (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source             TEXT NOT NULL CHECK (source IN
                             ('mik','rekordbox','djay','serato','traktor',
                              'open-dj-tool','manual','inferred','webui')),
        source_row_id      TEXT NOT NULL,
        field_name         TEXT NOT NULL,
        value_json         TEXT NOT NULL,
        unmatched_reason   TEXT NOT NULL CHECK (unmatched_reason IN
                             ('no_candidate','ambiguous_candidates',
                              'lost_collision')),
        confidence         REAL CHECK (confidence IS NULL OR
                                       (confidence >= 0 AND confidence <= 1)),
        title              TEXT,
        artist             TEXT,
        album              TEXT,
        isrc               TEXT,
        duration_ms        INTEGER,
        source_path        TEXT,
        modified_at        TEXT NOT NULL,
        imported_at        TEXT NOT NULL,
        promoted_stable_id TEXT REFERENCES tracks(stable_id) ON DELETE SET NULL,
        promoted_at        TEXT,
        UNIQUE (source, source_row_id, field_name)
    );
CREATE TABLE users (
        google_sub  TEXT PRIMARY KEY,
        email       TEXT NOT NULL,
        name        TEXT,
        avatar_url  TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
CREATE INDEX idx_tracks_isrc ON tracks(isrc) WHERE isrc IS NOT NULL;
CREATE INDEX idx_tracks_file_path ON tracks(file_path);
CREATE INDEX idx_track_vendor_ids_vendor ON track_vendor_ids(vendor, vendor_id);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_stable_id ON events(stable_id) WHERE stable_id IS NOT NULL;
CREATE INDEX idx_track_field_history_lookup ON track_field_history(stable_id, field_name, superseded_at);
CREATE UNIQUE INDEX idx_users_email ON users(email);
CREATE INDEX idx_auth_sessions_sub ON auth_sessions(google_sub);
CREATE INDEX idx_auth_sessions_expires ON auth_sessions(expires_at);
CREATE INDEX idx_local_changelog_table ON local_changelog(table_name, row_pk);
CREATE INDEX idx_track_locations_stable ON track_locations(stable_id);
CREATE UNIQUE INDEX idx_track_locations_path ON track_locations(stable_id, machine_id, kind, file_path) WHERE file_path IS NOT NULL;
CREATE UNIQUE INDEX idx_track_locations_url ON track_locations(stable_id, machine_id, kind, remote_url) WHERE remote_url IS NOT NULL;
CREATE INDEX idx_track_availability_state ON track_availability(state);
CREATE VIEW tracks_available AS
    SELECT t.*
    FROM tracks t
    JOIN track_availability a ON a.stable_id = t.stable_id
    WHERE a.state = 'present' AND t.deleted_at IS NULL;
CREATE VIEW tracks_unavailable AS
    SELECT t.*, a.state AS availability_state, a.checked_at AS availability_checked_at
    FROM tracks t
    JOIN track_availability a ON a.stable_id = t.stable_id
    WHERE a.state <> 'present' AND t.deleted_at IS NULL;
CREATE VIEW track_fields_available AS
    SELECT f.*
    FROM track_fields f
    JOIN track_availability a ON a.stable_id = f.stable_id
    JOIN tracks t ON t.stable_id = f.stable_id
    WHERE a.state = 'present' AND f.deleted_at IS NULL AND t.deleted_at IS NULL;
CREATE INDEX idx_unmatched_source_analysis_pending ON unmatched_source_analysis(source, field_name) WHERE promoted_stable_id IS NULL;
CREATE INDEX idx_unmatched_source_analysis_promoted ON unmatched_source_analysis(promoted_stable_id) WHERE promoted_stable_id IS NOT NULL;
CREATE INDEX idx_track_energy_segments_start ON track_energy_segments(stable_id, start_ms);
CREATE INDEX idx_machine_owners_sub ON machine_owners(google_sub);
CREATE INDEX idx_enrollment_grants_expires ON enrollment_grants(expires_at);
CREATE INDEX idx_lyric_verdict_red ON lyric_verdict(pct_witness_red DESC);
CREATE UNIQUE INDEX idx_playlist_memberships_item_id ON playlist_memberships(item_id);
CREATE INDEX idx_hub_changelog_table ON hub_changelog(table_name, row_pk);
CREATE INDEX idx_path_availability_checked ON path_availability(checked_at);
CREATE INDEX idx_tracks_audio_hash ON tracks(audio_hash) WHERE audio_hash IS NOT NULL;
CREATE INDEX idx_tracks_content_hash ON tracks(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX idx_tracks_isrc_upper ON tracks(upper(isrc));
CREATE INDEX idx_pending_tracks_playlist ON pending_tracks(playlist_id);
CREATE INDEX idx_pending_tracks_isrc ON pending_tracks(isrc) WHERE isrc IS NOT NULL;
CREATE INDEX idx_pending_tracks_status ON pending_tracks(status);
CREATE INDEX idx_spotify_playlist_links_odj ON spotify_playlist_links(odj_playlist_id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('track_field_history',0);
INSERT INTO "sqlite_sequence" VALUES('local_changelog',137);
INSERT INTO "sqlite_sequence" VALUES('pending_tracks',12);
INSERT INTO "sqlite_sequence" VALUES('events',22);
COMMIT;
