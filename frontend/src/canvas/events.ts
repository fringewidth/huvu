// Fired when huvu "speaks" (real TTS eventually, the mock test-voice clip
// for now) so GardenCanvas can react without Chat needing a ref into it.
export const HUVU_SPEAK_EVENT = "huvu:speak";

// Fired by GardenCanvas once huvu stops talking, whether the clip ended or
// the user talked over it. Voice mode waits on this before listening again.
export const HUVU_SPOKE_EVENT = "huvu:spoke";

// Fired as huvu's written (text mode) reply streams in, with the number of
// new words. GardenCanvas grows the garland a little per word.
export const HUVU_WROTE_EVENT = "huvu:wrote";
