# Synthetic test records

`apify-detailed-sales.json` is entirely synthetic. Names, provider IDs, prices, sizes, coordinates, and history were constructed for offline tests; they are not descriptions of actual homes. Source URLs are schema placeholders and must not be used to retrieve listings.

The three records cover exact prices differing from rounded display prices, older sale/rental history, a detached home with an attached garage, and an explicitly attached home that must be rejected. Keep these semantic distinctions when changing the fixture. Tests never import these records into the application's runtime dataset.
