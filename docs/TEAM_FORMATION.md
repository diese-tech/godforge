# Team Formation

GodForge forms equal SMITE teams from every supported even roster: 1v1, 2v2,
3v3, 4v4, or 5v5. It does not rely on ForgeLens, official ranks, or another
service. Odd rosters remain in ready check with guidance to wait for one
player, drop one player, or use a substitute.

The private match channel offers three persistent organizer choices:

- **Role Fit Teams** maximizes first choices, then second choices, and reports
  assignments outside either preference as unavoidable fills.
- **Balanced Teams** preserves the composition and role-preference priorities,
  then minimizes the visible strength difference.
- **Captain Teams** selects two captain volunteers deterministically and uses a
  snake pick order that favors uncovered roles before strength and stable ID
  tie-breakers.

Preferred composition per side is:

- 1 player: best available role balance.
- 2 players: one tank and one DPS.
- 3 players: one tank and two DPS.
- 4 players: two tanks and two DPS.
- 5 players: two tanks and three DPS, with one Solo, Jungle, Mid, Support, and
  ADC when feasible.

Solo and Support are tank roles; Jungle, Mid, and ADC are DPS roles. If the
roster cannot satisfy preferences cleanly, GodForge creates the closest stable
split and reports unavoidable fills instead of abandoning formation.

The selected mode, role assignments, preference satisfaction, strength
difference, and captain pick order are retained in the party draft snapshot.
Given the same inputs, results are identical regardless of input ordering.

Draft launch, assignments, results, room controls, and the optional one-click
team voice move stay in the private match channel. The shared Play card is only
a compact status projection and is not required for progression. Saved private
controls are reconciled on restart.

Strength is intentionally simple and explainable:

```text
organizer skill-band base + min(experience, 100) + recent adjustment
```

Missing skill bands use the intermediate base. This is a recreational
organizer input, not an inferred official rank or a global reputation score.
