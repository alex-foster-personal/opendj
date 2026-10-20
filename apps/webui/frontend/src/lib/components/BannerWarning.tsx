// React twin of BannerWarning.svelte: same DOM, held to BannerWarning.contract.ts.
export type BannerWarningProps = { message: string };

export default function BannerWarning({ message }: BannerWarningProps) {
	return (
		<div className="banner-warning" role="alert">
			{message}
		</div>
	);
}
