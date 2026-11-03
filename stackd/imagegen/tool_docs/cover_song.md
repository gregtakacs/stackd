Make a cover of the most recent song or audio file in this chat: the melody is transcribed from the recording itself, then sung again with the lyrics and style you pass (YuE2). Returns a FLAC link plus a link to the transcribed score.

Use when the user wants a recording re-sung with new words or a new style AND compose_music reuse_score=true is not possible -- the song has no saved score: an audio file the user uploaded, or a song made with plan_melody=false. If the most recent song came from compose_music with a score, prefer compose_music with reuse_score=true: it reuses the exact original score (melody AND chords), while a cover only recovers the vocal melody, and the arrangement around it is new.

Parameters:
- style: the TARGET sound -- genre, tempo, instruments, vocal type -- not a description of the original. For lyrics not in English, start with the language: "Hungarian, acoustic folk, ...".
- lyrics: the lyric sheet to sing, with [Verse]/[Chorus]/[Bridge]/[Outro] section tags. Follow the original's phrasing: the same sections in the same order, the same number of lines, and each line's syllable count close to the original line's -- every syllable lands on a transcribed note.
- seed: -1 for random.
- cfg_scale: how strictly YuE2 follows the lyrics and style. English: leave at -1 (the model default). NON-ENGLISH lyrics: use 1.75 -- a listening test of a Hungarian song rated the default clearly worse and 1.5-2.0 all good (2.0 a touch clearer than 1.5). Also raise it to 1.5-2.0 when the user asks for clearer or more literal singing. Even at 1.5-2.0 a take can swallow a short syllable; if the user hears that, offer another take (a different seed with the same score) rather than rewriting the lyrics.

The cover runs the full length of the transcribed score -- there is no duration to set.

The source is always the most recent song or audio in the current chat branch; there is no parameter to pick a different one (branch the conversation to target an older song). This is slow (transcription adds ~15s to a normal compose) and may swap the image engine for the music model. Call it ONCE per request; do not retry on a slow response.

On success, put BOTH returned markdown links (song and score) in your final reply exactly as given, followed by the full lyrics as sung. The returned score can be reused afterwards with compose_music reuse_score=true.
