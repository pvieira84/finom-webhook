# Finom fee split webhook for Firefly III

Splits a fee-inclusive EUR withdrawal into the original expense and Finom's fee.
Example: EUR 203.00 -> EUR 200.00 original expense + EUR 3.00 bank fee.
The account balance and withdrawal total remain unchanged.

The fee is 1.5% of the original expense: net = round(total / 1.015, 2),
fee = total - net. Uses decimal arithmetic and half-up rounding.
This assumes every eligible withdrawal includes that fee with no minimum,
cap, or other charge. Confirm this matches your Finom transactions.

## Setup with Docker

1. Create an **expense account** in Firefly III named `Finom fees`.
   Record its ID and the ID of your Finom **asset account** (visible in each
   account's URL). The fee account must be a separate expense account.
2. Create a Firefly III personal access token under your profile's OAuth settings.
   Keep the token in `.env`; do not share it in chat or commit it to git.
3. Copy `.env.example` to `.env` and replace the URL, token and account IDs.
   `FIREFLY_URL` is the instance root, without `/api/v1`.
4. In **Firefly III**, create an active webhook:
   - Trigger: transaction created (`STORE_TRANSACTION`).
   - Response/content: transactions (`TRANSACTIONS`).
   - Delivery: JSON.
   - URL: `https://your-webhook-host.example/webhook`.
   Copy the webhook's generated secret into `WEBHOOK_SECRET` in `.env`.
5. Run `docker compose up -d --build` from this folder.
   Put your existing HTTPS reverse proxy in front of `127.0.0.1:8080`.
   Forward POST bodies and the `Signature` header unchanged; do not log bodies.
   The supplied service is intentionally small and handles one request at a time.
   Run only one container/replica for a given Finom account.
6. Keep `DRY_RUN=true` initially. Trigger the webhook on one imported transaction
   using Firefly's webhook controls, or import a new transaction, then inspect
   `docker compose logs finom-webhook`. A EUR 203 example should show
   `dry_run net=200.00 fee=3.00`. No transaction is changed in this mode.
7. Set `DRY_RUN=false`, run `docker compose up -d --force-recreate`, and trigger
   the same transaction again. Verify both splits and the unchanged total in
   Firefly III before relying on it for future imports.

If Firefly is also in Docker, `localhost` inside its container refers to that
container. Use a reachable HTTPS hostname, or attach this service to the same
private Docker network and use `http://finom-webhook:8080/webhook`. The published
host port alone does not connect Docker networks. Configure internal API URLs
similarly. Firefly may restrict private webhook destinations; use its documented
trusted destination configuration if needed.

## Behavior

- Fetches current transaction state from `/api/v1/transactions/{group-id}`;
  updates that same group with PUT. Does not create a second transaction group.
- Filters by the configured source account ID and withdrawal type. This includes
  manually created withdrawals on that account too, not just Data Importer ones.
- Retains the original journal ID and changes only its amount, order and tags.
  Original description, category, budget, notes and bank references stay there.
- Adds a new fee journal without an existing journal ID, with the same source,
  date and currency, the fee expense account, and category `Bank fees`.
- Skips existing splits, tagged processed transactions, reconciled transactions,
  foreign currency transactions, non-EUR amounts, and zero/negative amounts.
  Fees that round to zero are also skipped, as are totals that cannot equal
  a cent-rounded purchase plus its cent-rounded 1.5% fee.
- Adds `finom-fee-split-v1` to both rows, preserving existing original tags.
  Retries refetch state and skip processed transactions. Updates disable rules
  and outgoing webhooks. A timeout after a successful PUT is safe to retry.
- Verifies Firefly's HMAC SHA3-256 signature over the raw body, with a five minute
  clock tolerance. Keep the host clocks synchronized.
- Returns an error if processing fails; inspect webhook delivery status and
  resend failed messages after fixing the cause. Do not assume automatic retries.

The handler cannot distinguish a fee-free Finom withdrawal from a fee-inclusive
one just by its total. Add an eligibility condition before enabling it if some
withdrawals do not have the 1.5% charge.

Creating this webhook does not automatically process historical transactions.
Use Firefly's trigger-transaction webhook API/control for selected older groups.
Avoid editing/importing the same transaction concurrently with this service.
Data Importer deduplication should be verified on a sample re-import: the service
keeps the bank reference on the original journal but changes its amount.

## Verification and limits

Run `python -m unittest -v` in this folder. Tests cover signatures, rounding and
total conservation, filters, dry-run, API payload structure and duplicate delivery.
They use a simulated Firefly API, not a live Firefly instance. Validate against
your installed Firefly version with one sample before enabling all imports.

References checked when building this service:
- https://docs.firefly-iii.org/how-to/firefly-iii/features/webhooks/
- https://docs.firefly-iii.org/references/firefly-iii/api/specials/
- https://github.com/firefly-iii/firefly-iii/blob/main/app/Helpers/Webhook/Sha3SignatureGenerator.php
- https://github.com/firefly-iii/firefly-iii/blob/main/app/Generator/Webhook/StandardMessageGenerator.php
