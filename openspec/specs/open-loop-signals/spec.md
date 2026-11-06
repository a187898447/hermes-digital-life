# open-loop-signals Specification

## Purpose
TBD - created by archiving change restore-digital-life-signals. Update Purpose after archive.
## Requirements
### Requirement: Bounded open-loop prompt context
The plugin SHALL append the highest-tension open gaps as one bounded suffix to existing system text after memory reordering, without adding a message or changing user content. The pure append operation SHALL accept an explicit reference timestamp for elapsed-age formatting and SHALL return its input byte-for-byte when there are no open gaps.

#### Scenario: No open gaps leave system text unchanged
- **WHEN** the pure open-loop append operation receives system text and a gap list with no open entries
- **THEN** it returns the exact original system text

#### Scenario: Open gaps are appended in tension order
- **WHEN** the prompt contains system text and more than one open gap
- **THEN** the plugin appends up to the three highest-tension gaps after existing memory reordering, with each gap's tension, summary, and elapsed age, and leaves user content unchanged

#### Scenario: Open-loop suffix is bounded
- **WHEN** formatted open-gap context would exceed 400 characters
- **THEN** lower-tension entries are omitted before higher-tension entries and the appended suffix does not exceed 400 characters

#### Scenario: No extra message is created
- **WHEN** open gaps are added to a request
- **THEN** the plugin modifies existing system text only and does not add or alter a user message

### Requirement: Assistant uncertainty creates an internal gap
The plugin SHALL inspect completed assistant responses for a meaningful sentence that expresses first-person uncertainty or cognitive inquiry, reject self-referential mechanism and expository sentences, and open or reactivate a gap through the existing state transition with source `assistant_unknown_claim`. It SHALL ignore slash-prefixed commands and empty or non-meaningful replies, avoid whole-response fallback, and fail silently if detection or persistence raises an exception.

The extraction structure SHALL follow the adopted upstream pipeline: sentence splitting, candidate cleaning/selection (including Markdown table-cell selection), meaningful-content filtering, self-reference/expository filtering, gap-worthiness filtering, and longest-candidate selection with a 100-character maximum. The Chinese unknown/inquiry/expository keyword sets are task-defined B-class rules that borrow the upstream structure; they are not a verbatim port of the upstream mixed Chinese/English regular expressions.

#### Scenario: Eligible uncertainty opens a gap
- **WHEN** a completed assistant response contains a meaningful first-person uncertainty or cognitive inquiry sentence
- **THEN** the runtime records or reactivates one gap with source `assistant_unknown_claim`

#### Scenario: Self-reference and exposition are rejected
- **WHEN** a candidate sentence discusses gap mechanisms or uses an expository construction such as “意味着” or “也就是说”
- **THEN** the response does not create a gap from that sentence

#### Scenario: Non-meaningful and command replies are ignored
- **WHEN** the assistant response is empty, begins with `/`, or has no candidate meeting the meaningful-content threshold
- **THEN** no assistant-derived gap is recorded

#### Scenario: Similar uncertainty does not create a twin gap
- **WHEN** an equivalent or sufficiently similar assistant uncertainty is detected again
- **THEN** the existing gap is reactivated or retained and a second open gap is not created

#### Scenario: Detection or persistence fails
- **WHEN** any exception occurs during assistant-response extraction or gap recording
- **THEN** the hook returns normally and does not disrupt reply processing

