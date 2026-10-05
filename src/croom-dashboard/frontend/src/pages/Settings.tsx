import { FormEvent, useState } from 'react';
import { useAuthStore } from '../store/auth';
import { authApi } from '../services/api';

const inputClass = 'w-full px-4 py-3 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500';

function ChangePasswordForm() {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMessage(null);
    setError(null);
    if (next.length < 8) {
      setError('Use at least 8 characters.');
      return;
    }
    if (next !== again) {
      setError('The new passwords do not match.');
      return;
    }
    setSaving(true);
    try {
      await authApi.changePassword(current, next);
      setMessage('Password changed.');
      setCurrent('');
      setNext('');
      setAgain('');
    } catch (err: any) {
      setError(err?.response?.data?.error || 'Could not change the password.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={submit} className="mt-6 space-y-4 max-w-md" aria-label="Change password">
      <h3 className="font-medium">Change password</h3>
      {error && <div className="bg-red-900/50 border border-red-500 text-red-200 px-4 py-3 rounded">{error}</div>}
      {message && <div className="bg-green-900/50 border border-green-500 text-green-200 px-4 py-3 rounded">{message}</div>}
      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">Current password</label>
        <input type="password" className={inputClass} value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">New password</label>
        <input type="password" className={inputClass} value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" required />
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">New password again</label>
        <input type="password" className={inputClass} value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="new-password" required />
      </div>
      <button type="submit" disabled={saving} className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors disabled:opacity-50">
        {saving ? 'Saving…' : 'Change password'}
      </button>
    </form>
  );
}

export default function Settings() {
  const { user } = useAuthStore();

  return (
    <div className="space-y-6">
      <div>
        <p className="kicker">Dashboard settings</p>
        <h1 className="text-3xl font-bold">Settings</h1>
        <p className="text-gray-400 mt-1">Dashboard configuration</p>
      </div>

      {/* Account */}
      <div className="bg-gray-800 rounded-lg p-6">
        <h2 className="text-lg font-medium mb-4">Account</h2>
        <dl className="space-y-4">
          <div className="flex justify-between">
            <dt className="text-gray-400">Name</dt>
            <dd>{user?.name}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-gray-400">Email</dt>
            <dd>{user?.email}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-gray-400">Role</dt>
            <dd className="capitalize">{user?.role}</dd>
          </div>
        </dl>
        <ChangePasswordForm />
      </div>

      {/* Dashboard Settings */}
      <div className="bg-gray-800 rounded-lg p-6">
        <h2 className="text-lg font-medium mb-4">Dashboard Settings</h2>
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p>Auto-refresh</p>
              <p className="text-sm text-gray-400">Automatically refresh device status</p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" className="sr-only peer" defaultChecked />
              <div className="w-11 h-6 bg-gray-600 rounded-full peer peer-checked:bg-blue-600"></div>
            </label>
          </div>
          <div className="flex items-center justify-between">
            <div>
              <p>Notifications</p>
              <p className="text-sm text-gray-400">Receive alerts for device issues</p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" className="sr-only peer" defaultChecked />
              <div className="w-11 h-6 bg-gray-600 rounded-full peer peer-checked:bg-blue-600"></div>
            </label>
          </div>
        </div>
      </div>

      {/* System Info */}
      <div className="bg-gray-800 rounded-lg p-6">
        <h2 className="text-lg font-medium mb-4">System Information</h2>
        <dl className="space-y-4">
          <div className="flex justify-between">
            <dt className="text-gray-400">Dashboard Version</dt>
            <dd>2.0.0-dev</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-gray-400">API Endpoint</dt>
            <dd className="text-xs font-mono">{window.location.origin}/api</dd>
          </div>
        </dl>
      </div>
    </div>
  );
}
