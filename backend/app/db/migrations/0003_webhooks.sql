-- GitHub webhook deliveries already processed (replay protection by X-GitHub-Delivery id).
CREATE TABLE webhook_deliveries (
    delivery_id     TEXT PRIMARY KEY,
    repo_id         TEXT,
    event           TEXT NOT NULL,
    received_at     TEXT NOT NULL
);
