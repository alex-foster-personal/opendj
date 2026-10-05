/** The slice of Element the anchor walk reads; tests pass plain objects. */
export interface AnchorishElement {
  id?: string;
  tagName?: string;
  getAttribute?(name: string): string | null;
  classList?: { length: number; item(i: number): string | null };
  parentElement?: AnchorishElement | null;
}
