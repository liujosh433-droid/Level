This is an app for busy caregivers and single parents who are looking
for enhanced support to manage their hectic Google Calendar
schedule and responsibilities.

To start, the user syncs their Google Calendar and allows Level to read/write
calendar events and also write emails. 

Once Level can read the calendar, it'll start reading a months worth of data (
    actually lets make this an easily toggleable/variable amount
    so we can change it letter to refine accuracy or spend less AI credits
    when testing
) 
surrounding the current date to infer care roles: ex, self, kids, elder care,
co-parents. 

We also want to read X amount of calendar data surrounding now to infer
"usual/repeating" events, so we can ask the user things like 
"You usually have kid A's school pickup today but it's not on your calendar
this week" => User can choose "Put it back", "This week is different", etc 

We also want to listen to the user and keep track of their priorities. THe 
priorities will become important when the user asks us to "find the best time" 
or "book an event" when we have to weigh decisions. 

Users should be able to chat with Level (through typing, or voice message). 
Users should also be able to hear a short summary of their day (Level 
speaks out loud).

I want to keep the same UI theme/style colors as Level has now. 

We want to minimize token usage at this point, so we should cache things
and not always call AI when not needed. 

We should by synced with calendar, so anytime an event changes/happens, 
we need to re-pull that. We also want to make sure our Priorities, Usuals, are 
synced with calendar dynamically with a reasonable sync + caching boundary.

We also want to allow Users to tell us reminders, so that we remember and remind
them when relevant events happen. Ex: "I forgot to bring Theo's soccer shoes to practice". 
Whenever we see/infer a Theo sports activity, we will remember and display
that reminder. 

Lastly, we want to be able to support tracking contacts of inferred people/care roles. 
Ex: You (doctor, other), Kid (teacher, doctor, other), Elder care (doctor, other)

We use this so Users can say things like "Send Nova's teacher an email asking
for excused sick absence". Gemini should draft something generic, courteous, 
and allow user to preview/edit before sending. 

Gemini will play a big role in this competition, so we need to make sure
our data process + storage type and syncing are best-suited for the features. 

Ex: How do we store/represent our User Profile, care-roles + their Usuals, 
priorities, reminders, so that they are easily retrieved (in terms of 
relevance and performance) when a feature requires its knowledge. 

We also need to continously update User data for calendar changes, user
feedback in chatbox. 

Given these features and requirements, propose an architectural flow, focusing
on each feature and how it syncs, interacts with data, and how data is stored, 
and how often/when. 

If there are design tradeoffs, please ask questions. And throughout the code, 
lets have best software practices: DRY, KISS, modular, bug-free, with
well-partitioned, thorough but concise documented tests for unit + e2e. 

The app should be easy-to-use, friendly (but level-headed), not 
overwhelming, and easy to find information. 

Let's also remember to follow all the guidelines of the competition: https://allthingsagentichackathon.devpost.com/rules
so we can score high and have a shot at winning. 

DO NOT use conditional imports when not necessary
