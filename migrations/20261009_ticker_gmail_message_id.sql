-- Store the Gmail message id after Approve & send.
-- Live crmbrain.ticker lacked this column; PostgREST rejected the whole
-- sent PATCH (PGRST204) and left nurture_state=in_progress.
-- Apply on project azpapwtnrbzywlnxxecz, schema crmbrain.

alter table crmbrain.ticker
  add column if not exists gmail_message_id text;
