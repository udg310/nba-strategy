CREATE TABLE ledger_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), account TEXT NOT NULL, environment TEXT NOT NULL,
 starting_capital INTEGER NOT NULL CHECK(starting_capital>0)
);
CREATE TABLE intents (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, environment TEXT NOT NULL, game_id TEXT NOT NULL,
 channel TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('OPEN','CLOSE')), asset TEXT NOT NULL, market TEXT NOT NULL,
 position_id TEXT NOT NULL, reason TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), limit_price INTEGER NOT NULL,
 signal_at INTEGER NOT NULL, deadline INTEGER NOT NULL, state TEXT NOT NULL,
 reserved INTEGER NOT NULL CHECK(reserved>=0), filled INTEGER NOT NULL DEFAULT 0,
 cost INTEGER NOT NULL DEFAULT 0, fee INTEGER NOT NULL DEFAULT 0, last_check INTEGER NOT NULL,
 attempt_no INTEGER NOT NULL DEFAULT 0, attempt_id TEXT NOT NULL DEFAULT '', order_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX one_open ON intents(account,environment,game_id,channel) WHERE action='OPEN';
CREATE UNIQUE INDEX one_close ON intents(position_id) WHERE action='CLOSE';
CREATE TABLE close_requests (
 intent_id TEXT NOT NULL REFERENCES intents(id), signal_at INTEGER NOT NULL, amount INTEGER NOT NULL,
 limit_price INTEGER NOT NULL, deadline INTEGER NOT NULL, PRIMARY KEY(intent_id,signal_at)
);
CREATE INDEX pending_intents ON intents(state,last_check);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id), number INTEGER NOT NULL,
 state TEXT NOT NULL, order_id TEXT, matched_high_water INTEGER NOT NULL DEFAULT 0,
 amount INTEGER NOT NULL, limit_price INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(intent_id,number), UNIQUE(order_id)
);
CREATE UNIQUE INDEX one_inflight ON attempts(intent_id) WHERE state IN ('SUBMITTING','ACCEPTED','UNKNOWN','PARTIAL');
CREATE TABLE fills (
 order_id TEXT NOT NULL, id TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES intents(id),
 asset TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
 price INTEGER NOT NULL, cost INTEGER NOT NULL, fee INTEGER NOT NULL, time INTEGER NOT NULL,
 PRIMARY KEY(order_id,id)
);
CREATE INDEX fills_by_intent ON fills(intent_id);
CREATE TABLE events (
 seq INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id), attempt_id TEXT NOT NULL,
 at INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE INDEX events_by_intent ON events(intent_id,seq);
CREATE TABLE settlements (
 position_id TEXT PRIMARY KEY REFERENCES intents(id), venue_event TEXT NOT NULL, leg_id TEXT NOT NULL,
 shares INTEGER NOT NULL, payout INTEGER NOT NULL, fee INTEGER NOT NULL, actor TEXT NOT NULL,
 evidence TEXT NOT NULL, at INTEGER NOT NULL, UNIQUE(venue_event,leg_id)
);
