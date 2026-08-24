---
name: patient-narrative
version: 2
description: How to write ICH E3 12.3.2 narratives of deaths and serious adverse events.
---

# Patient safety narratives (ICH E3 12.3.2)

Each narrative must contain, in a short structured paragraph:

- Patient identifier, age, and sex. RULE:IDENTIFY_PATIENT
- Study drug and dose at the time of the event.
- The event term and CTCAE grade / intensity.
- Action taken with study drug (interrupted, reduced, withdrawn).
- Outcome (recovered, recovering, not recovered, fatal), including date of
  death for fatal events.
- Investigator causality assessment.

Facts come only from the subject's ADSL and ADAE records; register the
subject-record locators in the facts map. Never speculate beyond the
recorded data; events clearly unrelated to study drug may be described
briefly.

## House rules (learned)

- State event onset as a study day relative to first dose ("On study day N, ..."). RULE:STATE_ONSET_DAY
