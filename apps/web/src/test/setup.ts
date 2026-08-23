import "@testing-library/jest-dom/vitest";

/**
 * A working `localStorage`.
 *
 * The jsdom environment here provides the property but not its methods, so
 * every call throws -- and the session module catches storage failures on
 * purpose, because a browser in private mode really can refuse. The two
 * together mean the tests would pass while silently exercising the failure
 * path: `remember()` would appear to work and store nothing.
 *
 * That is precisely the shape of bug worth avoiding in a test harness, so the
 * storage is supplied rather than worked around.
 */
class MemoryStorage implements Storage {
  private entries = new Map<string, string>();

  get length(): number {
    return this.entries.size;
  }

  key(index: number): string | null {
    return [...this.entries.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.entries.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.entries.set(key, String(value));
  }

  removeItem(key: string): void {
    this.entries.delete(key);
  }

  clear(): void {
    this.entries.clear();
  }
}

Object.defineProperty(window, "localStorage", {
  value: new MemoryStorage(),
  configurable: true,
  writable: true,
});
