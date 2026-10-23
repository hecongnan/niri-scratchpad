{
  description = "Niri IPC scratchpad with a shared floating region";

  outputs = { self }: {
    homeManagerModules.default = import ./home-manager.nix;
    homeManagerModules.scratchpad = self.homeManagerModules.default;
  };
}
