# Board views

GitHub has no API for project views, so these nine (design 6.6) are made by hand in the board's
UI, exactly as written here. Each line under a view is one setting in the view's menu. Filters are
typed into the filter bar as shown. Delete the default "View 1" once these exist.

Every planning view (all but "Bugs to triage" and "Audit") includes `-label:needs-fields`, so an
issue with missing or invalid fields stays out of planning until it is fixed.

Built-in fields used: **Type** (the issue type), **Assignees** (the owner), **Labels**,
**Linked pull requests**, **Reviewers**.

## 1. Needs a human

- Layout: Table
- Filter: `is:open -label:needs-fields status:"Waiting for human","In review"`
- Group by: Type
- Sort: Created, ascending (oldest first)
- Fields shown: Title, Type, Status, Assignees, Repository, Peras, Linked pull requests

The filter bar cannot OR across fields, so this view relies on Status: a new Decision starts in
**Waiting for human** (set by the form automation), an issue whose PR awaits review is **In
review**, and Peras sets **Waiting for human** when a cycle pauses.

## 2. This week

- Layout: Board
- Column field: Status
- Filter: `-label:needs-fields iteration:@current`
- Fields shown on cards: Assignees, Discipline, Priority, Type

## 3. My work

- Layout: Table
- Filter: `is:open -label:needs-fields assignee:@me -status:Done`
- Group by: Status
- Sort: Priority, ascending (Urgent first)
- Fields shown: Title, Type, Status, Priority, Iteration, Repository

## 4. Backlog

- Layout: Table
- Filter: `is:open -label:needs-fields no:iteration -status:Done`
- Group by: Discipline
- Sort: Priority, ascending (Urgent first)
- Fields shown: Title, Type, Discipline, Phase, Priority, Assignees

## 5. By discipline

- Layout: Board
- Column field: Discipline
- Filter: `is:open -label:needs-fields -status:Done`
- Sort: Priority, ascending
- Fields shown on cards: Assignees, Status, Priority, Iteration

## 6. Roadmap

- Layout: Roadmap
- Dates: Iteration (start and end both from Iteration)
- Zoom: Quarter
- Filter: `-label:needs-fields -type:Bug,"Audit finding"`
- Group by: Phase
- Sort: Priority, ascending

## 7. Peras cycles

- Layout: Table
- Filter: `is:open type:Intent has:peras`
- Sort: Peras, ascending
- Fields shown: Title, Peras, Status, Priority, Assignees, Linked pull requests

The stage, risk and budget columns arrive with Peras in phase 2 (design 7.9); add them here then.

## 8. Bugs to triage

- Layout: Table
- Filter: `is:open type:Bug`
- Sort: Created, ascending
- Fields shown: Title, Repository, Priority, Assignees, Labels

A bug leaves this view when it is closed as triaged into an intent or a task.

## 9. Audit (phase 1 only)

- Layout: Table
- Filter: `is:open type:"Audit finding",Decision phase:1`
- Group by: Audit
- Sort: Severity, ascending (Critical first)
- Fields shown: Title, Type, Audit, Severity, Status, Assignees

Archive this view when phase 1 ends.
