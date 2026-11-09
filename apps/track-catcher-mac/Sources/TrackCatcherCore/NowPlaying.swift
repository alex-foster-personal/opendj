import Foundation

/// Now playing from Spotify or Apple Music, read through Apple Events.
/// The scripts only ask a player that is already running, so they never
/// launch Spotify or Music.
public enum NowPlaying {
    public enum Player: String, CaseIterable, Sendable {
        case spotify = "Spotify", music = "Music"

        public var source: CatchEntry.Source { self == .spotify ? .spotify : .music }
        public var bundleID: String { self == .spotify ? "com.spotify.client" : "com.apple.Music" }
    }

    public struct Track: Equatable, Sendable {
        public let player: Player
        public let title: String
        public let artist: String
        public let id: String?
    }

    /// AppleScript printing "playing<TAB>title<TAB>artist<TAB>id", or an
    /// empty string when the player is not running or not playing.
    public static func script(for player: Player) -> String {
        let idExpr = player == .spotify ? "id of current track" : "persistent ID of current track"
        return """
        if application "\(player.rawValue)" is running then
          tell application "\(player.rawValue)"
            if player state is playing then
              return "playing" & tab & (name of current track) & tab & (artist of current track) & tab & (\(idExpr))
            end if
          end tell
        end if
        return ""
        """
    }

    /// Parse the script's output; nil when nothing is playing.
    public static func parse(_ output: String, player: Player) -> Track? {
        let f = output.trimmingCharacters(in: .newlines).components(separatedBy: "\t")
        guard f.count >= 3, f[0] == "playing", !f[1].isEmpty else { return nil }
        let id = f.count > 3 && !f[3].isEmpty ? f[3] : nil
        return Track(player: player, title: f[1], artist: f[2], id: id)
    }
}
