/**
 * Metrics and analytics routes.
 */

import { Router, Response } from 'express';
import { Op } from 'sequelize';
import { Device, Metrics } from '../models';
import { AuthRequest } from '../middleware/auth';
import { logger } from '../services/logger';

export const metricsRouter = Router();

interface MeetingEvent {
  deviceId: string;
  type: string;
  data: object;
  timestamp: Date;
}

/** Each call from its meeting_joined and meeting_left (one meetingId), newest call first. The events come newest first, as the
 * route reads them; a call is paired from its joined forwards, so they are walked oldest first. A call still going is left out. */
export function pairMeetings(newestFirst: MeetingEvent[]) {
  const meetings: any[] = [];
  const joined: Map<string, any> = new Map();
  [...newestFirst].reverse().forEach((m) => {
    const { meetingId, platform } = m.data as any;
    const key = `${m.deviceId}-${meetingId}`;
    if (m.type === 'meeting_joined') {
      joined.set(key, { deviceId: m.deviceId, meetingId, platform, joinedAt: m.timestamp });
    } else if (m.type === 'meeting_left' && joined.has(key)) {
      const call = joined.get(key);
      joined.delete(key);
      const duration = new Date(m.timestamp).getTime() - new Date(call.joinedAt).getTime();
      meetings.push({ ...call, leftAt: m.timestamp, durationMinutes: Math.round(duration / 60000) });
    }
  });
  return meetings.reverse();
}

// Get metrics for a device
metricsRouter.get('/device/:deviceId', async (req: AuthRequest, res: Response) => {
  try {
    const { deviceId } = req.params;
    const { type, from, to, limit } = req.query;

    const where: any = { deviceId };

    if (type) {
      where.type = type;
    }

    if (from || to) {
      where.timestamp = {};
      if (from) {
        where.timestamp[Op.gte] = new Date(from as string);
      }
      if (to) {
        where.timestamp[Op.lte] = new Date(to as string);
      }
    }

    const metrics = await Metrics.findAll({
      where,
      order: [['timestamp', 'DESC']],
      limit: limit ? Number(limit) : 100,
    });

    res.json({
      deviceId,
      metrics: metrics.map((m) => ({
        id: m.id,
        type: m.type,
        timestamp: m.timestamp,
        data: m.data,
      })),
    });
  } catch (error) {
    logger.error('Error getting device metrics:', error);
    res.status(500).json({ error: 'Failed to get metrics' });
  }
});

// Get fleet-wide metrics summary
metricsRouter.get('/summary', async (req: AuthRequest, res: Response) => {
  try {
    const { period = '24h' } = req.query;

    // Calculate time range
    const now = new Date();
    let from: Date;

    switch (period) {
      case '1h':
        from = new Date(now.getTime() - 60 * 60 * 1000);
        break;
      case '24h':
        from = new Date(now.getTime() - 24 * 60 * 60 * 1000);
        break;
      case '7d':
        from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
        break;
      case '30d':
        from = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
        break;
      default:
        from = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    }

    // Get device count by status
    const devices = await Device.findAll();
    const statusCounts = {
      online: devices.filter((d) => d.status === 'online').length,
      offline: devices.filter((d) => d.status === 'offline').length,
      error: devices.filter((d) => d.status === 'error').length,
    };

    // Get meeting count
    const meetingMetrics = await Metrics.count({
      where: {
        type: 'meeting_joined',
        timestamp: { [Op.gte]: from },
      },
    });

    // Get occupancy stats
    const occupancyMetrics = await Metrics.findAll({
      where: {
        type: 'occupancy',
        timestamp: { [Op.gte]: from },
      },
      order: [['timestamp', 'DESC']],
      limit: 1000,
    });

    let totalOccupancy = 0;
    let peakOccupancy = 0;
    occupancyMetrics.forEach((m) => {
      const count = (m.data as any).count || 0;
      totalOccupancy += count;
      if (count > peakOccupancy) {
        peakOccupancy = count;
      }
    });

    const avgOccupancy = occupancyMetrics.length > 0 ? totalOccupancy / occupancyMetrics.length : 0;

    res.json({
      period,
      from: from.toISOString(),
      to: now.toISOString(),
      devices: {
        total: devices.length,
        ...statusCounts,
      },
      meetings: {
        total: meetingMetrics,
      },
      occupancy: {
        average: Math.round(avgOccupancy * 10) / 10,
        peak: peakOccupancy,
      },
    });
  } catch (error) {
    logger.error('Error getting metrics summary:', error);
    res.status(500).json({ error: 'Failed to get metrics summary' });
  }
});

// Get meeting statistics
metricsRouter.get('/meetings', async (req: AuthRequest, res: Response) => {
  try {
    const { from, to, deviceId } = req.query;

    const where: any = {
      type: { [Op.in]: ['meeting_joined', 'meeting_left'] },
    };

    if (from || to) {
      where.timestamp = {};
      if (from) {
        where.timestamp[Op.gte] = new Date(from as string);
      }
      if (to) {
        where.timestamp[Op.lte] = new Date(to as string);
      }
    }

    if (deviceId) {
      where.deviceId = deviceId;
    }

    const metrics = await Metrics.findAll({
      where,
      order: [['timestamp', 'DESC']],
      limit: 500,
    });

    const meetings = pairMeetings(metrics);

    res.json({
      meetings,
      totalMeetings: meetings.length,
      totalMinutes: meetings.reduce((sum, m) => sum + m.durationMinutes, 0),
    });
  } catch (error) {
    logger.error('Error getting meeting stats:', error);
    res.status(500).json({ error: 'Failed to get meeting stats' });
  }
});
