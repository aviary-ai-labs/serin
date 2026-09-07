# Importing your broker's activity export

Serin can read a broker's own activity export and turn it into a transaction
ledger — every buy, sell, dividend, deposit and fee, including **positions you
have already closed**.

That last part is the reason to bother. A holdings import only knows what you
own *right now*. It cannot see the stock you bought in March and sold in June,
so it cannot tell you how that trade went, and it cannot compute a return that
accounts for the money you added and took out along the way. The activity
export can.

Files from a broker on this page are **parsed exactly, on the server**. No AI
is involved and nothing leaves your instance — Serin recognises the format from
its header row and reads the columns directly. Anything Serin does not
recognise still goes through Smart Import's AI extraction, which is a good
fallback but a worse tool for a file that already has a schema.

---

## Robinhood

### Where to download it

You need the **Account activity report** as a CSV. This is not the same thing
as the monthly *account statements*, which are PDFs and are much harder to read
accurately — if you end up with a PDF, you are in the wrong place.

**On the web** (easiest, and the only place you can pick a wide date range
comfortably):

1. Sign in at [robinhood.com](https://robinhood.com).
2. Click your **account icon**, top right.
3. Choose **Reports and statements**.
4. Open the **Reports** tab — *not* Statements.
5. Click **Generate new report**.
6. Fill in the form:
    - **Account** — pick the one you want. If you have both an individual
      account and an IRA, they export separately; do them one at a time and
      import each.
    - **Report type** — **Account activity**.
    - **Date range** — start from the day you opened the account, or as early
      as Robinhood lets you go. See the note on history limits below.
7. Click **Generate report**.

The report is not instant. Robinhood prepares it in the background and it
appears in the same **Reports** list when it is ready — usually a minute or
two, occasionally longer for a multi-year range. You may also get an email.
Download it from that list; it arrives as a `.csv`.

**In the mobile app**, the same report lives under the **Account** tab →
**menu** → **Reports and statements** → **Reports** → **Generate new report**.
The steps are identical from there. The web version is still easier, because
downloading a file on a phone and then uploading it to Serin is more work than
it sounds.

### Check you got the right file

Open it in any text editor. The first line should look like this:

```
"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity","Price","Amount"
```

If it does, Serin will recognise it. If the first line looks like anything
else, Robinhood has changed the format — send it to
[support@serin.money](mailto:support@serin.money) and the parser gets updated.

### Then import it

In Serin: **Smart Import** → drop the CSV in → review the transactions →
**Import**.

Re-importing is safe. Every row gets a stable fingerprint, so uploading an
overlapping export later adds only what is new. You do not need to remember
where the last one stopped — pick a range that overlaps generously.

### What Serin does with each row

| Robinhood code | Becomes | Notes |
|---|---|---|
| `Buy`, `Sell` | buy / sell | Fee is recovered from the gap between quantity × price and the amount banked |
| `BTO`, `BTC`, `STO`, `STC` | buy / sell | Marked as options; a contract moves 100× its quoted price |
| `OEXP` | adjustment | An expired option — the position ends, no cash moves |
| `CDIV` | dividend | |
| `DTAX` | tax | Withholding on a dividend |
| `INT` | interest **or** fee | Interest earned is income; margin interest charged is a cost. Serin reads the sign |
| `ACH`, `RTP`, `WIRE` | deposit **or** withdrawal | Same — direction comes from the sign |
| `GOLD`, `DFEE`, `AFEE` | fee | Subscription and regulatory fees |
| `SPL`, `SPR` | split | |
| `REC` | transfer | Shares moving in or out without a sale |

**Anything Serin does not recognise is left out and named**, not guessed at.
After an import you will see a line listing any codes that were skipped. That
is deliberate: a ledger with a visible hole is far better than one with a
plausible wrong row in it, because only one of those is something you can spot
and fix. If you see a skipped code, send it to support and it gets added.

### Two things worth knowing before you trust the numbers

**Robinhood's export does not go back forever.** You can only export the range
Robinhood still holds. If you have held something since before that window,
its purchase is not in the file, and Serin will not invent one — the position
will show up in your holdings with no matching buy. Serin labels how far back
its transaction history actually reaches rather than implying a complete
picture, so check that label after importing.

**Transfers in from another broker (`REC`) carry no cost basis.** Robinhood
knows the shares arrived; it does not necessarily know what you paid for them
somewhere else. Those need their original purchase entering by hand, or
importing from the broker they came from.

---

## Other brokers

Not yet parsed exactly — but Smart Import's AI extraction reads most CSV, PDF
and screenshot exports well enough to review before importing, and it always
shows you what it read before anything is written.

If you would like your broker parsed properly, send a sample export — with the
amounts changed to anything you like, only the **header row and the transaction
codes** matter — to [support@serin.money](mailto:support@serin.money). A
parser is roughly an hour of work once the format is known.
