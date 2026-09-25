"""Default writing instructions (Settings → Writing instructions). The owner can edit all of these.
The locked rules live in writing.py and can't be edited."""

OLD_STYLE = """- Associated Press style: numerals for 10 and up, "Monday" not "this Monday", times as "7 p.m.", titles before names ("Mayor Adam Stockford").
- Plain words. Active voice. Name people and places the first time; use last names after that.
- Write for someone who lives here and is reading on a phone.
- Length: 250 to 500 words unless the material only supports less. Briefs can be 80 to 150 words."""

OLD_STRUCTURE = "1. Lead: one sentence, under 35 words, saying what happened, who it affects, and when. Put the most important new fact first.\n2. Why it matters: the second paragraph tells readers what this means for them.\n3. Details: the rest of the facts in order of importance. Short paragraphs of one to three sentences.\n4. Quotes: use the strongest real quote high in the story, after the facts it supports.\n5. Numbers: give context for every number (compared with last year, per resident, out of how many).\n6. What's next: end with the next date, meeting, deadline, or how readers can take part or find out more."

STRUCTURE = """1. Lead: one sentence, under 35 words, saying what happened, who it affects, and when. Put the most important new fact first.
2. Why it matters: the second paragraph tells readers what this means for them.
3. Details: the rest of the facts in order of importance. Short paragraphs of one to three sentences.
4. Quotes: use the strongest real quote high in the story, after the facts it supports.
5. Numbers: give context for every number (compared with last year, per resident, out of how many).
6. What's next: end with the next date, meeting, deadline, or how readers can take part or find out more, when the material has it."""

STYLE = """- Write like a good community newspaper reporter: clear, warm and specific. Lead with the most interesting or useful fact for readers, not with who announced it ("The Hillsdale women's basketball team will shoot free throws for a cause Friday", not "Hillsdale College published a video about…").
- Use the concrete details you have: names, places, times, numbers. Vary sentence length. Active verbs.
- Say where information came from naturally, once per source or where it matters. Don't start every sentence with "according to".
- Never write that something is unknown, unclear, unavailable or wasn't released, unless a source said it was withheld. Leave it out; it goes in the gaps list instead.
- Never pad. If the facts are few, write a short, complete story.
- Associated Press style: numerals for 10 and up, "Monday" not "this Monday", times as "7 p.m.", titles before names ("Mayor Adam Stockford").
- Write for someone who lives here and is reading on a phone.
- Length: as long as the facts support. A simple announcement is 120 to 200 words; a bigger story up to 500."""

BANNED = """"In a significant development", "it remains to be seen", "amid", "a testament to", "underscores", "highlights the importance", "navigating", "landscape", "bustling", "vibrant", "tight-knit community", "nestled", "delve", "pivotal", "crucial", "robust", "in today's world", "at the end of the day", "only time will tell", "residents are encouraged to", "stay tuned", "sent shockwaves". No exclamation marks. No rhetorical questions. No closing paragraph that sums up or moralises."""

HEADLINE = """- Under 70 characters. Subject, verb, the key fact. Present tense for things that just happened ("City council approves new water rates").
- Name the place if it isn't {town}. No clickbait, no questions, no puns on serious stories.
- Also write a social post of up to 200 characters and a one-sentence summary for the homepage."""

# key: (label, default extra instructions)
TEMPLATES = {
    "general": ("General news", ""),
    "brief": ("Brief", "80–150 words, three short paragraphs, no quotes needed."),
    "crime": ("Crime and courts", "Say who released the information. Use \"arrested\", \"charged\", \"alleged\". "
                                  "Include the charge, court date and bond if known."),
    "government": ("Government and meetings", "Lead with the decision, not that a meeting happened. Include the vote "
                                              "count, cost and when it takes effect."),
    "obituary": ("Obituary and memorial", "Warm, factual, no cause of death unless the family gave it. Service time "
                                          "and place at the end."),
    "sports": ("Sports", "Final score and key players in the lead. Records, standings and next game at the end."),
    "business": ("Business", "What's opening, changing or closing, where, when, and hours. No promotional language."),
    "event": ("Event preview", "What, when, where, cost, who it's for, how to sign up, in the first three paragraphs."),
    "weather": ("Weather and emergencies", "What to do right now first. Times, areas, official source. No drama."),
    "people": ("Community and people", "Tell it through the person. One strong detail or quote high up."),
    "national": ("National and world", "Say how it affects readers here if the material shows that. Otherwise write "
                                       "it straight."),
}

WRITING_GUIDE = """**How to write a news story people will read**

1. **Start with what happened.** Your first sentence says who did what, where and when.
2. **Then say why it matters** to people who live here.
3. **Keep paragraphs short:** one to three sentences.
4. **Say how you know.** "According to the fire chief…", "I was there and saw…".
5. **Quotes:** put quotation marks only around words someone actually said, and say who said them.
6. **Facts, not opinions.** Save opinions for a comment.
7. **End with what's next:** the next game, meeting, date or how to help.

Don't include home addresses or phone numbers of private people, and don't name anyone under 18 who is accused of a crime."""
