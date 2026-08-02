-- Bind an answer to a canonical question rather than to a phrasing of it.
--
-- Forms ask the same thing many ways: "When can you start?", "What is your
-- earliest start date?" and "Notice period?" are one question. Keyed only by
-- their text, they become three answer_bank rows that drift apart, and the
-- first form to phrase it a fourth way gets nothing.
--
-- Nullable: an answer Jarra types for a question the catalog does not know
-- still belongs in the bank, keyed by its text alone.
ALTER TABLE answer_bank ADD COLUMN question_key TEXT;
CREATE UNIQUE INDEX idx_answer_bank_key ON answer_bank(question_key)
  WHERE question_key IS NOT NULL;

-- Same on the gap list, so answering a question removes every phrasing of it
-- rather than the one that happened to be recorded.
ALTER TABLE unmapped_questions ADD COLUMN question_key TEXT;
