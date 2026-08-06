{
  description = "Personal CLI tools and Claude Code environment";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          tools = import ./nix/tools.nix { inherit pkgs; };
        in
        tools
        // {
          default = pkgs.symlinkJoin {
            name = "tools";
            paths = builtins.attrValues tools;
          };
        }
      );

      overlays.default = final: _prev: import ./nix/tools.nix { pkgs = final; };

      homeManagerModules.default = import ./nix/hm-module.nix;
    };
}
