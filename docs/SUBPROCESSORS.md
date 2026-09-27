# Subprocessors

Keep this list current; customers' security teams will ask for it by name. The same list is served
in-product at `/api/subprocessors` (Admin page) from `sereno/config.py`. Update both together.

| Subprocessor | Purpose | Data processed | Location | Terms |
|---|---|---|---|---|
| Anthropic, PBC (Claude API) | AI model inference: reading documents and returning structured data | Page images and PDF text of submitted documents, transiently per request | United States | Commercial terms; API inputs/outputs not used for model training. Confirm current retention terms (and zero-data-retention eligibility) with Anthropic before signing a customer that requires it |
| Cloud host (to confirm per deployment; target AWS ap-south-1 Mumbai) | Hosting, encrypted object storage, managed Postgres, KMS | Encrypted documents (time-boxed), extracted data, audit logs | India (target) | Provider DPA |

Not used: no analytics/tracking scripts, no third-party fonts or CDNs in the web app, no other AI vendors.
Change procedure: notify customers 30 days before adding a subprocessor (put this in the MSA).
