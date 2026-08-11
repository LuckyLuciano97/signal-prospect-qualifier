"""Alternative ways to get pages in front of the detectors.

The live path is ``net.PoliteClient``, which fetches. This package holds the
offline paths, which do not. They exist so crawling can move somewhere else
(an n8n workflow, a scheduled job, a colleague's machine) without any
detector, score, opener or validation check changing.

The boundary that makes that safe: **a source hands over raw fetched pages,
never extracted signals.** Evidence quotes are substring-validated against
page text, so a source that pre-extracts "findings" would leave the
validator with nothing to validate against and the evidence guarantee would
quietly become a promise.
"""
