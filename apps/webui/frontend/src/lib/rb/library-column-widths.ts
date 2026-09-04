const ORDER_COLUMN_FLOOR_PX = 24;
const ORDER_DIGIT_WIDTH_PX = 5;
const ORDER_COLUMN_PADDING_PX = 12;

/** Returns the smallest order-column width that fits its largest row number. */
export function compactOrderWidth(maxRowOrder: number): number {
	if (!Number.isSafeInteger(maxRowOrder) || maxRowOrder < 0) {
		throw new Error('maxRowOrder must be a non-negative safe integer');
	}
	const digitCount = String(maxRowOrder).length;
	return Math.max(ORDER_COLUMN_FLOOR_PX, digitCount * ORDER_DIGIT_WIDTH_PX + ORDER_COLUMN_PADDING_PX);
}
