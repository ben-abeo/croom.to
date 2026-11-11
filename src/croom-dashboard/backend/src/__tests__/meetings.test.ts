import { pairMeetings } from '../routes/metrics';

const at = (minute: number) => new Date(Date.UTC(2026, 10, 10, 9, minute));
const event = (type: string, meetingId: string, platform: string, minute: number) => ({
  deviceId: 'd1', type, data: { meetingId, platform }, timestamp: at(minute),
});

describe('pairMeetings', () => {
  it('pairs each call from the events as the database hands them, newest first, and lists the newest call first', () => {
    const newestFirst = [
      event('meeting_joined', 'c', 'zoom', 55),               // a call still going: not counted yet
      event('meeting_left', 'b', 'zoom', 50),
      event('meeting_joined', 'b', 'zoom', 40),
      event('meeting_left', 'a', 'google_meet', 30),
      event('meeting_joined', 'a', 'google_meet', 0),
    ];
    expect(pairMeetings(newestFirst)).toEqual([
      { deviceId: 'd1', meetingId: 'b', platform: 'zoom', joinedAt: at(40), leftAt: at(50), durationMinutes: 10 },
      { deviceId: 'd1', meetingId: 'a', platform: 'google_meet', joinedAt: at(0), leftAt: at(30), durationMinutes: 30 },
    ]);
  });
});
